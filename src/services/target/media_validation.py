"""Bounded local media evidence, independent of source admission and SQL.

Matrix source: Meta IG User /media.
https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media
FFmpeg parser interface: deployment Nixpkgs archive / FFmpeg 7.1.

Resource limits/private cwd are NOT a filesystem sandbox. MOV external-track
references are refused before using explicit disabled dref/path options. No
network, transformations, captions, URLs or raw decoder diagnostics are retained.
Video bitrate semantics/conformance remain unproven, so a video that passes
every probe stays `validation_pending` (`needs_spec_evidence`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import subprocess
import os
import resource
import signal
import shutil
import struct
import sys
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from .vocabulary import PUBLISH_MAX_BYTES

MATRIX_VERSION = "instagram-story-2026-10-11-v1"
EVIDENCE_VERSION = 1
SUPPORTED_MEDIA_MATRIX = MappingProxyType(
    {
        "version": MATRIX_VERSION,
        "source": "https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media",
        "checked": "2026-10-11",
        "provider_page_updated": "2026-09-28",
        "color_evidence": "Story Image Specifications explicitly permits conversion to sRGB",
        "byte_caps": MappingProxyType(
            {
                "image": min(PUBLISH_MAX_BYTES["image"], 8_000_000),
                "video": min(PUBLISH_MAX_BYTES["video"], 100_000_000),
            }
        ),
        "image": ("JPEG", "sRGB; provider converts other color spaces"),
        "video": (
            "MOV/MP4",
            "h264/hevc",
            "progressive",
            "closed GOP",
            "4:2:0",
            "front moov",
            "no edit lists",
        ),
        "duration_seconds": (3, 60),
        "fps": (23, 60),
        "width": 1920,
        "aspect": ("1/10", "10"),
        "audio": (
            "AAC",
            "sample rate <=48000Hz",
            "mono/stereo",
            "128kbps interpretation unproven",
        ),
        "video_bitrate": "VBR <=25000000 bits/s: measurement and coded-stream conformance unproven",
        "provider_video_bytes": 100_000_000,
    }
)


@dataclass(frozen=True)
class ValidationInput:
    data: bytes = field(repr=False)
    kind: Literal["image", "video"]
    source_generation: str
    observation_revision: int
    selected_asset_id: str

    def __post_init__(self):
        if not isinstance(self.data, bytes) or self.kind not in ("image", "video"):
            raise ValueError("immutable bytes and known media kind required")
        identities = (self.source_generation, self.selected_asset_id)
        if (
            any(
                not isinstance(value, str)
                or not 0 < len(value) <= 256
                or "://" in value
                for value in identities
            )
            or type(self.observation_revision) is not int
            or self.observation_revision < 0
        ):
            raise ValueError("captured observation identity required")


@dataclass(frozen=True)
class ValidationLimits:
    wall_seconds: float = 30
    cpu_seconds: int = 15
    memory_bytes: int = 768 * 1024 * 1024
    output_bytes: int = 32 * 1024 * 1024
    pixels: int = 8_000_000
    frames: int = 10_000
    boxes: int = 20_000
    depth: int = 12
    ffprobe: str = "ffprobe"
    ffmpeg: str = "ffmpeg"
    temporary_root: str | None = None

    def __post_init__(self):
        ceilings = {
            "wall_seconds": 30,
            "cpu_seconds": 15,
            "memory_bytes": 768 * 1024 * 1024,
            "output_bytes": 32 * 1024 * 1024,
            "pixels": 8_000_000,
            "frames": 10_000,
            "boxes": 20_000,
            "depth": 12,
        }
        for name, ceiling in ceilings.items():
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 < value <= ceiling
            ):
                raise ValueError("validation limits may only be tightened")
            if name != "wall_seconds" and not isinstance(value, int):
                raise ValueError("integer resource limit required")


@dataclass(frozen=True)
class ValidationResult:
    state: Literal["supported", "unsupported", "validation_pending"]
    reason: str
    source_generation: str
    observation_revision: int
    selected_asset_id: str
    fingerprint: str
    evidence: Mapping[str, object]
    matrix_version: str = MATRIX_VERSION
    evidence_version: int = EVIDENCE_VERSION
    #: For a result that is not supported: the check that decided it and the
    #: values it measured (`{"check", "measured"}`). Not part of the fingerprint.
    failed_check: Mapping[str, object] | None = None


class ValidationPending(Exception):
    """Closed safe diagnostic, never the raw tool exception."""


class UnsupportedMedia(Exception):
    pass


#: Bounds on a measured value kept as text, and on a list of them.
_MEASURED_TEXT = 64
_MEASURED_ITEMS = 32


def _measured_value(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, (list, tuple)):
        return [_measured_value(item) for item in value[:_MEASURED_ITEMS]]
    return str(value)[:_MEASURED_TEXT]


def _measured(exc: Exception, **values) -> Exception:
    """`exc` carrying the values that decided it, as `exc.measured`.

    Whole numbers stay numbers; a Fraction is kept as its exact text and any
    other value as bounded text. These are the parsed facts a check read,
    never the tool's raw output.
    """
    exc.measured = {key: _measured_value(value) for key, value in values.items()}
    return exc


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)


def _resource_limits(limits):
    resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


async def _await_owned_cleanup(cleanup):
    """Complete a retained cleanup task despite repeated caller cancellation."""
    cancellation = None
    while True:
        try:
            await asyncio.shield(cleanup)
            return cancellation
        except asyncio.CancelledError as exc:
            if cleanup.cancelled():
                raise
            if cancellation is None:
                cancellation = exc


async def _reap_owned_process(process, pipe_tasks=()):
    # The group remains ours if its leader exits while a child holds a pipe.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for task in pipe_tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*pipe_tasks, return_exceptions=True)
    # Drain bounded pipe buffers after killing the group, then reap the leader.
    await process.communicate()


async def _reap_creation(creation):
    await _reap_owned_process(await creation)


async def run_bounded(argv, limits, cwd, deadline):
    """Own only this process group; enforce one deadline and aggregate output cap."""
    executable = shutil.which(argv[0])
    if executable is None:
        raise ValidationPending("tool_unavailable")
    if sys.platform != "linux":
        raise ValidationPending("resource_limits_unavailable")
    try:
        creation = asyncio.create_task(
            asyncio.create_subprocess_exec(
                executable,
                *argv[1:],
                cwd=cwd,
                env={
                    "PATH": os.defpath,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
                    "LANG": "C",
                },
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=lambda: _resource_limits(limits),
            )
        )
        try:
            process = await asyncio.shield(creation)
        except asyncio.CancelledError as cancellation:
            cleanup = asyncio.create_task(_reap_creation(creation))
            try:
                await _await_owned_cleanup(cleanup)
            finally:
                # Preserve the first cancellation, including its original message.
                raise cancellation
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationPending("tool_unavailable") from exc
    used = 0

    async def collect(stream):
        nonlocal used
        chunks = []
        while chunk := await stream.read(16_384):
            used += len(chunk)
            if used > limits.output_bytes:
                raise ValidationPending("output_budget")
            chunks.append(chunk)
        return b"".join(chunks)

    tasks = [
        asyncio.create_task(collect(process.stdout)),
        asyncio.create_task(collect(process.stderr)),
    ]
    completion = asyncio.gather(*tasks, process.wait())
    cancellation = None
    try:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ValidationPending("deadline")
        try:
            stdout, stderr, _ = await asyncio.wait_for(completion, remaining)
        except asyncio.TimeoutError as exc:
            raise ValidationPending("deadline") from exc
        return _ProcessResult(process.returncode, stdout, stderr)
    except asyncio.CancelledError as exc:
        cancellation = exc
        raise
    finally:
        cleanup = asyncio.create_task(
            _reap_owned_process(process, [*tasks, completion])
        )
        interrupted = await _await_owned_cleanup(cleanup)
        if interrupted is not None and cancellation is None:
            raise interrupted


@dataclass(frozen=True)
class ContainerEvidence:
    reason: str | None
    pending: bool = False
    media_extents: tuple[tuple[int, int], ...] = ()
    sample_codecs: tuple[str, ...] = ()
    #: For a refusal: the box list and the sample codecs walked so far.
    measured: Mapping[str, object] = field(default_factory=dict)


def inspect_container(
    data: bytes, limits: ValidationLimits | None = None
) -> ContainerEvidence:
    """Small bounded ISO-BMFF walker; opaque payload text never becomes a box."""
    limits = limits or ValidationLimits()
    count = 0
    top = []
    tracks = []
    extents = []
    codecs = []
    pending = False

    def boxes(start, end, depth):
        nonlocal count
        if depth > limits.depth:
            raise ValidationPending("container_budget")
        while start < end:
            count += 1
            if count > limits.boxes:
                raise ValidationPending("container_budget")
            if end - start < 8:
                raise UnsupportedMedia("malformed_container")
            size, kind = struct.unpack_from(">I4s", data, start)
            header = 8
            if size == 1:
                if end - start < 16:
                    raise UnsupportedMedia("malformed_container")
                size = struct.unpack_from(">Q", data, start + 8)[0]
                header = 16
            elif size == 0:
                size = end - start
            if size < header or size > end - start:
                raise UnsupportedMedia("malformed_container")
            yield kind, start, start + header, start + size
            start += size

    def walk(start, end, depth, track=None):
        nonlocal pending
        for kind, offset, payload, finish in boxes(start, end, depth):
            if depth == 0:
                top.append((kind, offset, finish))
            if kind == b"mdat" and depth == 0:
                extents.append((payload, finish))
            if kind == b"elst":
                raise UnsupportedMedia("edit_list")
            if kind in (b"cmov", b"moof", b"mvex"):
                pending = True
            if kind == b"trak":
                track = {"refs": [], "indices": []}
                tracks.append(track)
                walk(payload, finish, depth + 1, track)
            elif kind in (b"moov", b"mdia", b"minf", b"dinf", b"stbl", b"edts"):
                walk(payload, finish, depth + 1, track)
            elif kind in (b"dref", b"stsd"):
                if track is None or finish - payload < 8:
                    raise ValidationPending("container_evidence_incomplete")
                n = struct.unpack_from(">I", data, payload + 4)[0]
                entries = list(boxes(payload + 8, finish, depth + 1))
                if len(entries) != n:
                    raise UnsupportedMedia("malformed_container")
                for entry, _, content, end_entry in entries:
                    if kind == b"dref":
                        if end_entry - content < 4:
                            raise UnsupportedMedia("malformed_container")
                        flags = int.from_bytes(data[content : content + 4], "big")
                        if entry in (b"url ", b"urn ", b"alis") and flags != 1:
                            raise UnsupportedMedia("external_reference")
                        track["refs"].append(entry == b"url " and flags == 1)
                    else:
                        if end_entry - content < 8:
                            raise UnsupportedMedia("malformed_container")
                        track["indices"].append(
                            struct.unpack_from(">H", data, content + 6)[0]
                        )
                        codecs.append(entry.decode("ascii", errors="replace"))

    try:
        walk(0, len(data), 0)
        moovs = [b for b in top if b[0] == b"moov"]
        mdats = [b for b in top if b[0] == b"mdat"]
        if len(moovs) != 1 or not mdats:
            raise ValidationPending("container_evidence_incomplete")
        if moovs[0][2] > mdats[0][1]:
            raise UnsupportedMedia("moov_not_front")
        for track in tracks:
            if not track["indices"] or not track["refs"]:
                pending = True
            for index in track["indices"]:
                if not 0 < index <= len(track["refs"]) or not track["refs"][index - 1]:
                    pending = True
        if not tracks or pending:
            raise ValidationPending("container_evidence_incomplete")
        if any(
            codec not in ("avc1", "avc3", "hvc1", "hev1", "mp4a") for codec in codecs
        ):
            raise UnsupportedMedia("video_codec")
        return ContainerEvidence(
            None, media_extents=tuple(extents), sample_codecs=tuple(codecs)
        )
    except (UnsupportedMedia, ValidationPending) as exc:
        measured = {
            "boxes": [kind.decode("ascii", errors="replace") for kind, _, _ in top],
            "box_count": count,
            "sample_codecs": codecs,
            **getattr(exc, "measured", {}),
        }
        return ContainerEvidence(
            str(exc),
            pending=isinstance(exc, ValidationPending),
            measured=_measured(exc, **measured).measured,
        )


def _image_worker(path, pixels):
    """Called only in the resource-bounded owned Python subprocess."""
    import warnings
    from PIL import Image, ImageCms, ImageFile, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = pixels
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            if Path(path).read_bytes()[:2] != b"\xff\xd8":
                # A recognizable other image is a format refusal, garbage is malformed.
                try:
                    with Image.open(path) as other:
                        other.verify()
                        found = other.format
                except (UnidentifiedImageError, OSError, ValueError):
                    raise UnsupportedMedia("malformed_image") from None
                raise _measured(UnsupportedMedia("image_format"), format=found)
            with Image.open(path, formats=["JPEG"]) as image:
                if image.width * image.height > pixels:
                    raise _measured(
                        ValidationPending("pixel_budget"),
                        width=image.width,
                        height=image.height,
                        pixel_limit=pixels,
                    )
                image.verify()
            with Image.open(path, formats=["JPEG"]) as image:
                image.load()
                if image.info.get("icc_profile"):
                    try:
                        ImageCms.ImageCmsProfile(
                            __import__("io").BytesIO(image.info["icc_profile"])
                        )
                    except (OSError, ValueError, ImageCms.PyCMSError):
                        raise ValidationPending("color_space_unproven") from None
                print(
                    json.dumps(
                        {
                            "width": image.width,
                            "height": image.height,
                            "color": "provider_conversion_allowed",
                            "decode": "complete",
                        }
                    )
                )
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        print(json.dumps({"pending": "pixel_budget"}))
    except ValidationPending as exc:
        print(
            json.dumps({"pending": str(exc), "measured": getattr(exc, "measured", {})})
        )
    except (UnsupportedMedia, UnidentifiedImageError, OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, UnsupportedMedia) else "malformed_image"
        print(
            json.dumps(
                {"unsupported": reason, "measured": getattr(exc, "measured", {})}
            )
        )


async def validate_media(
    source: ValidationInput, *, limits: ValidationLimits | None = None
) -> ValidationResult:
    limits = limits or ValidationLimits()
    evidence = {"bytes": len(source.data)}
    if len(source.data) <= SUPPORTED_MEDIA_MATRIX["byte_caps"][source.kind]:
        evidence["sha256"] = hashlib.sha256(source.data).hexdigest()
    state, reason = "supported", "validated"
    try:
        cap = SUPPORTED_MEDIA_MATRIX["byte_caps"][source.kind]
        if len(source.data) > cap:
            raise _measured(
                UnsupportedMedia("byte_limit"), bytes=len(source.data), cap=cap
            )
        deadline = asyncio.get_running_loop().time() + limits.wall_seconds
        container = None
        if source.kind == "video":
            container = inspect_container(source.data, limits)
            if container.reason:
                raise _measured(
                    (ValidationPending if container.pending else UnsupportedMedia)(
                        container.reason
                    ),
                    **container.measured,
                )
        with tempfile.TemporaryDirectory(
            prefix="media-validation-", dir=limits.temporary_root
        ) as directory:
            local = Path(directory) / "asset"
            local.write_bytes(source.data)
            local.chmod(0o400)
            if source.kind == "image":
                command = [
                    sys.executable,
                    "-c",
                    "from src.services.target.media_validation import _image_worker;import sys;_image_worker(sys.argv[1],int(sys.argv[2]))",
                    str(local),
                    str(limits.pixels),
                ]
                output = await run_bounded(command, limits, directory, deadline)
                if output.returncode:
                    raise ValidationPending("decoder_unavailable")
                try:
                    facts = json.loads(output.stdout)
                except (ValueError, UnicodeError):
                    raise ValidationPending("incomplete_evidence") from None
                measured = facts.pop("measured", None)
                measured = measured if isinstance(measured, dict) else {}
                if "pending" in facts:
                    raise _measured(ValidationPending(facts["pending"]), **measured)
                if "unsupported" in facts:
                    raise _measured(UnsupportedMedia(facts["unsupported"]), **measured)
                evidence.update(facts)
            else:
                evidence.update(
                    await _validate_video(local, container, limits, directory, deadline)
                )
                raise ValidationPending("needs_spec_evidence")
    except (UnsupportedMedia, ValidationPending) as exc:
        state = (
            "unsupported" if isinstance(exc, UnsupportedMedia) else "validation_pending"
        )
        reason = str(exc)
        failed_check = MappingProxyType(
            {"check": reason, "measured": dict(getattr(exc, "measured", {}))}
        )
    else:
        failed_check = None
    fingerprint_data = {
        "kind": source.kind,
        "generation": source.source_generation,
        "revision": source.observation_revision,
        "asset": source.selected_asset_id,
        "matrix": MATRIX_VERSION,
        "evidence_version": EVIDENCE_VERSION,
        "state": state,
        "reason": reason,
        "evidence": evidence,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ValidationResult(
        state,
        reason,
        source.source_generation,
        source.observation_revision,
        source.selected_asset_id,
        fingerprint,
        MappingProxyType(evidence),
        failed_check=failed_check,
    )


def _integer(value):
    if isinstance(value, bool):
        raise ValidationPending("incomplete_evidence")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValidationPending("incomplete_evidence") from None


def _ratio(value):
    try:
        ratio = Fraction(str(value).replace(":", "/"))
        if ratio <= 0:
            raise ValueError
        return ratio
    except (ValueError, ZeroDivisionError, TypeError):
        raise ValidationPending("incomplete_evidence") from None


def inspect_video_facts(facts, formats, container, limits):
    streams = facts.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) > 1 or len(streams) != len(videos) + len(audios):
        raise _measured(
            ValidationPending("stream_topology"),
            streams=len(streams),
            video_streams=len(videos),
            audio_streams=len(audios),
        )
    video = videos[0]
    if video.get("codec_name") not in ("h264", "hevc"):
        raise _measured(
            UnsupportedMedia("video_codec"), video_codec=video.get("codec_name")
        )
    if video.get("side_data_list"):
        raise ValidationPending("transform_evidence_incomplete")
    index = _integer(video.get("index"))
    frames = [f for f in facts.get("frames", []) if f.get("stream_index") == index]
    packets = [p for p in facts.get("packets", []) if p.get("stream_index") == index]
    # show_entries emits interleaved packets_and_frames in FFmpeg 7.1.
    if "packets_and_frames" in facts:
        frames = [
            f
            for f in facts["packets_and_frames"]
            if f.get("type") == "frame" and f.get("stream_index") == index
        ]
        packets = [
            p
            for p in facts["packets_and_frames"]
            if p.get("type") == "packet" and p.get("stream_index") == index
        ]
    if (
        not frames
        or len(frames) != _integer(video.get("nb_frames"))
        or len(packets) != len(frames)
    ):
        raise ValidationPending("incomplete_evidence")
    if (
        len(frames) > limits.frames
        or len(facts.get("packets_and_frames", [])) > limits.frames * 4
    ):
        raise ValidationPending("frame_budget")
    time_base = _ratio(video.get("time_base"))
    timestamps = []
    dimensions = set()
    for frame in frames:
        width, height = _integer(frame.get("width")), _integer(frame.get("height"))
        if width <= 0 or height <= 0:
            raise ValidationPending("incomplete_evidence")
        if width > 1920:
            raise _measured(UnsupportedMedia("width"), width=width, height=height)
        if width * height > limits.pixels:
            raise _measured(
                ValidationPending("pixel_budget"),
                width=width,
                height=height,
                pixel_limit=limits.pixels,
            )
        sar = _ratio(frame.get("sample_aspect_ratio"))
        aspect = Fraction(width, height) * sar
        if not Fraction(1, 10) <= aspect <= 10:
            raise _measured(
                UnsupportedMedia("aspect"),
                width=width,
                height=height,
                sample_aspect_ratio=sar,
                aspect=aspect,
            )
        dimensions.add((width, height, sar))
        if _integer(frame.get("interlaced_frame")) != 0:
            raise _measured(UnsupportedMedia("interlaced"), width=width, height=height)
        pix = formats.get(frame.get("pix_fmt"))
        if pix is None:
            raise ValidationPending("color_evidence_incomplete")
        if pix != (1, 1):
            raise _measured(
                UnsupportedMedia("chroma_subsampling"), pix_fmt=frame.get("pix_fmt")
            )
        timestamps.append((_integer(frame.get("pts")), _integer(frame.get("duration"))))
    if len(dimensions) != 1:
        raise ValidationPending("geometry_evidence_incomplete")
    timestamps.sort()
    durations = {duration for _, duration in timestamps}
    if len(durations) != 1 or next(iter(durations)) <= 0:
        raise ValidationPending("timing_evidence_incomplete")
    tick = next(iter(durations))
    if any(b[0] - a[0] != tick for a, b in zip(timestamps, timestamps[1:])):
        # No undocumented interpretation of VFR or discontinuities.
        raise ValidationPending("timing_evidence_incomplete")
    duration = (timestamps[-1][0] + tick - timestamps[0][0]) * time_base
    fps = 1 / (tick * time_base)
    timing = dict(duration_seconds=duration, fps=fps, video_frames=len(frames))
    if not 3 <= duration <= 60:
        raise _measured(UnsupportedMedia("duration"), **timing)
    if not 23 <= fps <= 60:
        raise _measured(UnsupportedMedia("fps"), **timing)
    if _integer(video.get("duration_ts")) * time_base != duration:
        raise ValidationPending("timing_evidence_incomplete")
    packet_timings = sorted(
        (_integer(p.get("pts")), _integer(p.get("duration"))) for p in packets
    )
    if packet_timings != timestamps:
        raise ValidationPending("timing_evidence_incomplete")
    all_packets = facts.get("packets", []) or [
        p for p in facts.get("packets_and_frames", []) if p.get("type") == "packet"
    ]
    for packet in all_packets:
        pos, size = _integer(packet.get("pos")), _integer(packet.get("size"))
        if size <= 0 or not any(
            start <= pos and pos + size <= end for start, end in container.media_extents
        ):
            raise _measured(
                UnsupportedMedia("packet_outside_media"),
                packet_pos=pos,
                packet_size=size,
            )
    audio_evidence = "absent_spec_interpretation_unproven"
    if audios:
        audio = audios[0]
        if audio.get("codec_name") != "aac":
            raise _measured(
                UnsupportedMedia("audio_codec"), audio_codec=audio.get("codec_name")
            )
        if _integer(audio.get("sample_rate")) > 48000:
            raise _measured(
                UnsupportedMedia("audio_sample_rate"),
                sample_rate=_integer(audio.get("sample_rate")),
            )
        if _integer(audio.get("channels")) not in (1, 2):
            raise _measured(
                UnsupportedMedia("audio_channels"),
                channels=_integer(audio.get("channels")),
            )
        audio_evidence = "unproven"
    width, height, sar = next(iter(dimensions))
    return {
        "codec": video["codec_name"],
        "video_frames": len(frames),
        "width": width,
        "height": height,
        "sample_aspect_ratio": str(sar),
        "duration_seconds": str(duration),
        "fps": str(fps),
        "progressive": True,
        "chroma": "4:2:0",
        "video_bitrate": "unproven",
        "audio_bitrate": audio_evidence,
        "container": "front_moov_no_edits_self_contained",
        "average_video_bps_diagnostic_only": str(
            Fraction(sum(_integer(p.get("size")) for p in packets) * 8, 1) / duration
        ),
    }


def inspect_headers(trace, codec, packet_count):
    # Pin textual syntax to FFmpeg 7.1; validate all packets, not just extradata.
    packet_sections = re.split(r"\[trace_headers @ [^]]+\] Packet:", trace)[1:]
    if len(packet_sections) != packet_count:
        raise ValidationPending("header_evidence_incomplete")
    idr_types = (5,) if codec == "h264" else (19, 20)
    slices = []
    for number, section in enumerate(packet_sections):
        types = [
            int(n) for n in re.findall(r"\bnal_unit_type\s+[01]+ = (\d+)", section)
        ]
        if codec == "h264" and any(
            n not in (1, 5, 6, 7, 8, 9, 10, 11, 12) for n in types
        ):
            raise ValidationPending("header_evidence_incomplete")
        actual_slices = (
            [n for n in types if n in (1, 5)]
            if codec == "h264"
            else [n for n in types if n < 32]
        )
        if not actual_slices:
            raise ValidationPending("header_evidence_incomplete")
        intra = [int(n) for n in re.findall(r"\bslice_type\s+[01]+ = (\d+)", section)]
        is_intra = (
            any(n % 5 in (2, 4) for n in intra)
            if codec == "h264"
            else any(n == 2 for n in intra)
        )
        if (number == 0 or "key frame" in section.splitlines()[0] or is_intra) and any(
            n not in idr_types for n in actual_slices
        ):
            raise ValidationPending("closed_gop_unproven")
        slices.extend(actual_slices)
    if codec == "hevc" and any(
        int(n) != 0 for n in re.findall(r"\bnuh_layer_id\s+[01]+ = (\d+)", trace)
    ):
        raise ValidationPending("header_evidence_incomplete")
    evidence = {
        "closed_gop": "idr_starts_proven",
        "header_packets": packet_count,
        "bitrate_conformance": "unproven",
    }

    # Retain declarations as declarations only. Changes/multiple schedules are unknown.
    def values(name):
        return [
            int(n)
            for n in re.findall(r"\b" + re.escape(name) + r"\s+[01]+ = (\d+)", trace)
        ]

    nal_hrd = values("nal_hrd_parameters_present_flag")
    vcl_hrd = values("vcl_hrd_parameters_present_flag")
    if not nal_hrd or any(n != 1 for n in nal_hrd) or any(n != 0 for n in vcl_hrd):
        return evidence
    scales, rates = values("bit_rate_scale"), values("bit_rate_value_minus1[0]")
    cpb_scales, cpbs, modes = (
        values("cpb_size_scale"),
        values("cpb_size_value_minus1[0]"),
        values("cbr_flag[0]"),
    )
    if scales and rates and len(set(scales)) == len(set(rates)) == 1:
        evidence["declared_nal_hrd_max_bps"] = (rates[0] + 1) * 2 ** (6 + scales[0])
    if cpb_scales and cpbs and len(set(cpb_scales)) == len(set(cpbs)) == 1:
        evidence["declared_cpb_bits"] = (cpbs[0] + 1) * 2 ** (4 + cpb_scales[0])
    if modes and len(set(modes)) == 1:
        evidence["declared_hrd_schedule"] = (
            "intermittent" if modes[0] == 0 else "continuous"
        )
    return evidence


def _tool_success(output, *, decode=False):
    if output.returncode == 0:
        return
    if output.returncode < 0:
        raise ValidationPending("resource_budget")
    stderr = output.stderr.lower()
    # These errors establish malformed bytes; resource/capability failures do not.
    if decode and any(
        word in stderr
        for word in (
            b"corrupt",
            b"invalid data",
            b"error while decoding",
            b"error submitting a packet",
        )
    ):
        raise UnsupportedMedia("malformed_video")
    raise ValidationPending("decoder_unavailable" if decode else "probe_incomplete")


async def _validate_video(local, container, limits, directory, deadline):
    versions = []
    for tool in (limits.ffprobe, limits.ffmpeg):
        output = await run_bounded([tool, "-version"], limits, directory, deadline)
        _tool_success(output)
        version = (
            output.stdout.decode("utf-8", errors="replace").splitlines()[0]
            if output.stdout
            else ""
        )
        if not re.match(r"(ffprobe|ffmpeg) version 7\.1(?: |$)", version):
            raise ValidationPending("tool_version_unproven")
        versions.append("7.1")
    pixels = await run_bounded(
        [
            limits.ffprobe,
            "-v",
            "error",
            "-show_pixel_formats",
            "-show_entries",
            "pixel_format=name,log2_chroma_w,log2_chroma_h",
            "-of",
            "json",
        ],
        limits,
        directory,
        deadline,
    )
    _tool_success(pixels)
    try:
        formats = {
            p["name"]: (p.get("log2_chroma_w"), p.get("log2_chroma_h"))
            for p in json.loads(pixels.stdout)["pixel_formats"]
        }
    except (ValueError, KeyError, TypeError):
        raise ValidationPending("incomplete_evidence") from None
    input_options = [
        "-f",
        "mov",
        "-protocol_whitelist",
        "file",
        "-enable_drefs",
        "0",
        "-use_absolute_path",
        "0",
        "-codec_whitelist",
        "h264,hevc,aac",
    ]
    entries = "stream=index,codec_type,codec_name,width,height,time_base,duration_ts,nb_frames,sample_rate,channels,sample_aspect_ratio:stream_side_data=side_data_type,rotation,displaymatrix:packet=stream_index,pts,duration,pos,size:frame=stream_index,pts,duration,width,height,pix_fmt,sample_aspect_ratio,interlaced_frame"
    output = await run_bounded(
        [
            limits.ffprobe,
            "-v",
            "error",
            *input_options,
            "-threads",
            "1",
            "-err_detect",
            "explode",
            "-max_streams",
            "3",
            "-max_pixels",
            str(limits.pixels),
            "-probesize",
            "40000000",
            "-analyzeduration",
            "61000000",
            "-show_entries",
            entries,
            "-of",
            "json",
            str(local),
        ],
        limits,
        directory,
        deadline,
    )
    _tool_success(output)
    if output.stderr:
        raise ValidationPending("probe_incomplete")
    try:
        facts = json.loads(output.stdout)
        evidence = inspect_video_facts(facts, formats, container, limits)
    except (ValueError, TypeError, KeyError):
        raise ValidationPending("incomplete_evidence") from None
    audio = any(s.get("codec_type") == "audio" for s in facts["streams"])
    decode_command = [
        limits.ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "info",
        "-xerror",
        "-max_error_rate",
        "0",
        "-filter_threads",
        "1",
        *input_options,
        "-threads",
        "1",
        "-err_detect",
        "explode",
        "-max_pixels",
        str(limits.pixels),
        "-noautorotate",
        "-i",
        str(local),
    ]
    # Separate full passes prevent concurrent showinfo/ashowinfo log fragments
    # interleaving. Each track is decoded completely under the SAME deadline.
    decode = await run_bounded(
        [
            *decode_command,
            "-map",
            "0:v:0",
            "-vf",
            "showinfo",
            "-fps_mode",
            "passthrough",
            "-f",
            "null",
            "-",
        ],
        limits,
        directory,
        deadline,
    )
    _tool_success(decode, decode=True)
    decoded = decode.stderr.decode("utf-8", errors="replace")
    video_frames = re.findall(
        r"\[Parsed_showinfo_[^]]+\] n:\s*\d+.*?fmt:(\S+).*?s:(\d+)x(\d+).*?i:(\S+)",
        decoded,
    )
    if len(video_frames) != evidence["video_frames"]:
        raise ValidationPending("decode_evidence_incomplete")
    for fmt, width, height, interlace in video_frames:
        if (
            formats.get(fmt) != (1, 1)
            or int(width) != evidence["width"]
            or int(height) != evidence["height"]
            or interlace != "P"
        ):
            raise ValidationPending("decode_evidence_incomplete")
    if audio:
        audio_decode = await run_bounded(
            [*decode_command, "-map", "0:a:0", "-af", "ashowinfo", "-f", "null", "-"],
            limits,
            directory,
            deadline,
        )
        _tool_success(audio_decode, decode=True)
        decoded_audio = audio_decode.stderr.decode("utf-8", errors="replace")
        audio_frames = re.findall(
            r"\[Parsed_ashowinfo_[^]]+\] n:\d+.*?channels:(\d+).*?rate:(\d+)",
            decoded_audio,
        )
        audio_index = next(
            s["index"] for s in facts["streams"] if s.get("codec_type") == "audio"
        )
        decoded_audio_count = sum(
            1 for f in facts.get("frames", []) if f.get("stream_index") == audio_index
        )
        if "packets_and_frames" in facts:
            decoded_audio_count = sum(
                1
                for f in facts["packets_and_frames"]
                if f.get("type") == "frame" and f.get("stream_index") == audio_index
            )
        if not audio_frames or len(audio_frames) != decoded_audio_count:
            raise ValidationPending("audio_evidence_incomplete")
        for channels, rate in audio_frames:
            if int(channels) not in (1, 2):
                raise UnsupportedMedia("audio_channels")
            if int(rate) > 48000:
                raise UnsupportedMedia("audio_sample_rate")
    headers = await run_bounded(
        [
            limits.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-loglevel",
            "info",
            "-xerror",
            *input_options,
            "-i",
            str(local),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-bsf:v",
            "trace_headers",
            "-f",
            "null",
            "-",
        ],
        limits,
        directory,
        deadline,
    )
    _tool_success(headers)
    evidence.update(
        inspect_headers(
            headers.stderr.decode("utf-8", errors="replace"),
            evidence["codec"],
            evidence["video_frames"],
        )
    )
    evidence.update(
        {
            "decode": "complete",
            "tool_versions": tuple(versions),
            "unmet_predicates": (
                "maximum_video_bitrate_definition",
                "whole_stream_bitrate_conformance",
                "audio_bitrate_interpretation"
                if audio
                else "silent_audio_interpretation",
            ),
        }
    )
    return evidence
