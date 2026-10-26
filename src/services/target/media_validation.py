"""Bounded local media evidence, independent of source admission and SQL.

Matrix source: Meta IG User /media.
https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media
FFmpeg interface: ffprobe's JSON stream and format entries.

Resource limits/private cwd are NOT a filesystem sandbox. MOV external-track
references are refused before using explicit disabled dref/path options. No
network, transformations, captions, URLs or raw decoder diagnostics are retained.

Meta never receives a video's source file. It fetches the transit store's
story derivation of it (`transit.story_transformation`, delivered as MP4 in the
story frame), so a video is judged only on what the derivation keeps from it:
its size, a well-formed self-contained container, a video stream the probe can
open, its duration and its frame rate. The derivation is trimmed to the
longest story and encoded to the matrix's story video
(`story_video_encoding`), so a longer video is kept and only a shorter one is
refused for its length. The frame rate is still refused outside the matrix:
the derivation's frame-rate range is not documented for a source outside it.
The source's container layout (edit lists, where `moov` sits), its encoding
settings and its audio settings are the derivation's to set, and are not
judged here.

The derivation itself is judged before Meta is asked to fetch it
(`judge_derivation`), on what the transit store's readiness probe read of it
(`derivation_container`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
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

MATRIX_VERSION = "instagram-story-2026-10-26-v3"
EVIDENCE_VERSION = 2
#: Meta: "File size: 100MB maximum"
PROVIDER_VIDEO_BYTES = 100_000_000
#: Meta: "File size: 8 MB maximum." (Story Image Specifications)
PROVIDER_IMAGE_BYTES = 8_000_000
SUPPORTED_MEDIA_MATRIX = MappingProxyType(
    {
        "version": MATRIX_VERSION,
        "source": "https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media",
        "checked": "2026-10-11",
        "provider_page_updated": "2026-09-28",
        "color_evidence": "Story Image Specifications explicitly permits conversion to sRGB",
        "byte_caps": MappingProxyType(
            {
                "image": min(PUBLISH_MAX_BYTES["image"], PROVIDER_IMAGE_BYTES),
                "video": min(PUBLISH_MAX_BYTES["video"], PROVIDER_VIDEO_BYTES),
            }
        ),
        "image": ("JPEG", "sRGB; provider converts other color spaces"),
        "video": (
            "MOV/MP4, well formed and self-contained",
            "a video stream the probe can open: H.264 or HEVC, with AAC audio",
            "delivered to Meta as the transit store's story derivation",
        ),
        # Meta: "Duration: 60 seconds maximum,  3 seconds minimum"
        "duration_seconds": (3, 60),
        # Meta: "Frame rate: 23-60 FPS."
        "fps": (23, 60),
        # What the story derivation is encoded to (`story_video_encoding`).
        # Meta: "Video codec: HEVC or H264, progressive scan, closed GOP, 4:2:0
        # chroma subsampling." H.264 high profile is 4:2:0, and level 4.2
        # holds 1080 x 1920 at up to 60 fps.
        "video_codec": ("h264", "high", "4.2"),
        # Meta: "Audio codec: AAC, 48khz sample rate maximum, 1 or 2 channels
        # (mono or stereo)."
        "audio_codec": "aac",
        "audio_hz": 48_000,
        "audio_channels": 2,
        # Meta: "Video bitrate: VBR, 25Mbps maximum"; "Audio bitrate: 128kbps"
        "provider_video_bits_per_second": 25_000_000,
        "provider_audio_bits_per_second": 128_000,
        "provider_video_bytes": PROVIDER_VIDEO_BYTES,
        "provider_image_bytes": PROVIDER_IMAGE_BYTES,
    }
)


def story_video_encoding() -> dict:
    """What every story video's derivation is encoded to, from the matrix, as
    a video's evidence records it. The bit-rate cap is the lower of Meta's two
    limits at the longest story: its VBR maximum, and its file size less the
    audio, rounded down to the megabit so the container's overhead fits."""
    matrix = SUPPORTED_MEDIA_MATRIX
    longest = matrix["duration_seconds"][1]
    file_rate = matrix["provider_video_bytes"] * 8 // longest
    fits_the_file = file_rate - matrix["provider_audio_bits_per_second"]
    return {
        "video_codec": list(matrix["video_codec"]),
        "audio_codec": matrix["audio_codec"],
        "audio_hz": matrix["audio_hz"],
        "fps": list(matrix["fps"]),
        "video_bits_per_second": min(
            matrix["provider_video_bits_per_second"],
            fits_the_file // 1_000_000 * 1_000_000,
        ),
        "longest_seconds": longest,
    }


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
    boxes: int = 20_000
    depth: int = 12
    ffprobe: str = "ffprobe"
    temporary_root: str | None = None

    def __post_init__(self):
        ceilings = {
            "wall_seconds": 30,
            "cpu_seconds": 15,
            "memory_bytes": 768 * 1024 * 1024,
            "output_bytes": 32 * 1024 * 1024,
            "pixels": 8_000_000,
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
    # Printable text only: a box kind or a codec name read from the file can
    # carry NUL, which jsonb refuses, or other control characters.
    text = "".join(c if c.isprintable() else "?" for c in str(value))
    return text[:_MEASURED_TEXT]


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
    #: For a refusal: the box list and the sample codecs walked so far.
    measured: Mapping[str, object] = field(default_factory=dict)


class _BoxBudget:
    """How many boxes a walk has read, against its limits' box and depth
    budget."""

    __slots__ = ("limits", "count")

    def __init__(self, limits: ValidationLimits):
        self.limits = limits
        self.count = 0


def _boxes(data: bytes, start: int, end: int, depth: int, budget: _BoxBudget):
    """The ISO-BMFF boxes between *start* and *end*, as (kind, payload start,
    end). A header cut short, or a box that runs past its parent, is
    `malformed_container`; a walk past the budget is `container_budget`.
    Opaque payload text never becomes a box."""
    if depth > budget.limits.depth:
        raise ValidationPending("container_budget")
    while start < end:
        budget.count += 1
        if budget.count > budget.limits.boxes:
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
        yield kind, start + header, start + size
        start += size


def inspect_container(
    data: bytes, limits: ValidationLimits | None = None
) -> ContainerEvidence:
    """Small bounded ISO-BMFF walker; opaque payload text never becomes a box."""
    budget = _BoxBudget(limits or ValidationLimits())
    top = []
    tracks = []
    codecs = []
    pending = False

    def walk(start, end, depth, track=None):
        nonlocal pending
        for kind, payload, finish in _boxes(data, start, end, depth, budget):
            if depth == 0:
                top.append(kind)
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
                entries = list(_boxes(data, payload + 8, finish, depth + 1, budget))
                if len(entries) != n:
                    raise UnsupportedMedia("malformed_container")
                for entry, content, end_entry in entries:
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
        if top.count(b"moov") != 1 or b"mdat" not in top:
            raise ValidationPending("container_evidence_incomplete")
        for track in tracks:
            if not track["indices"] or not track["refs"]:
                pending = True
            for index in track["indices"]:
                if not 0 < index <= len(track["refs"]) or not track["refs"][index - 1]:
                    pending = True
        if not tracks or pending:
            raise ValidationPending("container_evidence_incomplete")
        # The decoders the probe may open on untrusted bytes (`_PROBE_CODECS`):
        # a stream outside them is refused here rather than left to the probe.
        refused = [
            codec
            for codec in codecs
            if codec not in ("avc1", "avc3", "hvc1", "hev1", "mp4a")
        ]
        if refused:
            raise _measured(UnsupportedMedia("video_codec"), refused_codecs=refused)
        return ContainerEvidence(None)
    except (UnsupportedMedia, ValidationPending) as exc:
        measured = {
            "boxes": [kind.decode("ascii", errors="replace") for kind in top],
            "box_count": budget.count,
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
                    raise ValidationPending("pixel_budget")
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
                    await _validate_video(local, limits, directory, deadline)
                )
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


def _positive_fraction(value):
    """A probe's number (`12.5`, `30000/1001`) as an exact Fraction, or None
    where it states none: absent, `N/A`, `0/0` or not positive."""
    try:
        number = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return number if number > 0 else None


def inspect_video_facts(facts):
    """What the story derivation keeps of a video: its video stream (the first,
    as the derivation takes it), the stream's duration (else the container's)
    and its frame rate. A video shorter than the shortest story is refused; one
    longer than the longest is kept, trimmed by the derivation, and recorded as
    trimmed. A frame rate the probe does not state is not judged; one outside
    the matrix is refused."""
    streams = facts.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    if not videos:
        raise _measured(
            UnsupportedMedia("stream_topology"), streams=len(streams), video_streams=0
        )
    video = videos[0]
    duration = _positive_fraction(video.get("duration")) or _positive_fraction(
        facts.get("format", {}).get("duration")
    )
    if duration is None:
        raise ValidationPending("timing_evidence_incomplete")
    fps = _positive_fraction(video.get("avg_frame_rate"))
    timing = dict(duration_seconds=duration, fps=fps)
    shortest, longest = SUPPORTED_MEDIA_MATRIX["duration_seconds"]
    if duration < shortest:
        raise _measured(UnsupportedMedia("duration"), **timing)
    slowest, fastest = SUPPORTED_MEDIA_MATRIX["fps"]
    if fps is not None and not slowest <= fps <= fastest:
        raise _measured(UnsupportedMedia("fps"), **timing)
    return {
        "codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "duration_seconds": str(duration),
        "fps": "unstated" if fps is None else str(fps),
        "audio": any(s.get("codec_type") == "audio" for s in streams),
        "trimmed_to_seconds": str(longest) if duration > longest else None,
        "derivation": story_video_encoding(),
    }


#: The decoders the probe may open on untrusted bytes. The container walker
#: refuses a sample entry outside them before the probe runs.
_PROBE_CODECS = "h264,hevc,aac"


def _probe_succeeded(output):
    """Return when the probe ran clean. Words that establish malformed bytes
    refuse the file; a resource or capability failure leaves it pending."""
    if output.returncode < 0:
        raise ValidationPending("resource_budget")
    if output.returncode == 0 and not output.stderr:
        return
    stderr = output.stderr.lower()
    if any(
        word in stderr
        for word in (
            b"corrupt",
            b"invalid data",
            b"error while decoding",
            b"error submitting a packet",
        )
    ):
        raise UnsupportedMedia("malformed_video")
    raise ValidationPending("probe_incomplete")


async def _validate_video(local, limits, directory, deadline):
    output = await run_bounded(
        [
            limits.ffprobe,
            "-v",
            "error",
            "-f",
            "mov",
            "-protocol_whitelist",
            "file",
            "-enable_drefs",
            "0",
            "-use_absolute_path",
            "0",
            "-codec_whitelist",
            _PROBE_CODECS,
            "-threads",
            "1",
            "-err_detect",
            "explode",
            "-max_streams",
            "3",
            "-max_pixels",
            str(limits.pixels),
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,duration",
            "-of",
            "json",
            str(local),
        ],
        limits,
        directory,
        deadline,
    )
    _probe_succeeded(output)
    try:
        return inspect_video_facts(json.loads(output.stdout))
    except (ValueError, TypeError, AttributeError):
        raise ValidationPending("incomplete_evidence") from None


# -- the story derivation ------------------------------------------------------

#: AAC's sample entry in an MP4 (ISO/IEC 14496-14), as the derivation's
#: `stsd` names it.
_AAC_SAMPLE_ENTRY = "mp4a"
#: Whether each of three lines of Meta's refuses the derivation: "MOV or MP4
#: (MPEG-4 Part 14), no edit lists, moov atom at the front of the file." and
#: "1 or 2 channels (mono or stereo)". They describe an output nobody has
#: measured: ffmpeg's MP4 muxer, for one, writes an edit list on each H.264
#: and AAC track and puts `moov` after the media by default, and Cloudinary
#: has no parameter for the channel count. So each is recorded with the
#: derivation's other facts, and Meta's answer to the container call judges
#: it.
REFUSE_MOOV_POSITION = False
REFUSE_EDIT_LISTS = False
REFUSE_AUDIO_CHANNELS = False
#: An AudioSpecificConfig's sampling frequency index, as its rate (ISO/IEC
#: 14496-3); index 15 is an explicit 24-bit rate.
_AAC_RATES = (
    96_000,
    88_200,
    64_000,
    48_000,
    44_100,
    32_000,
    24_000,
    22_050,
    16_000,
    12_000,
    11_025,
    8_000,
    7_350,
)
#: Its channelConfiguration, as the channels it names; 0 leaves them to a
#: program config element, unread here.
_AAC_CHANNELS = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 8, 11: 7, 12: 8, 13: 24, 14: 8}


def _fourcc(kind: bytes) -> str:
    """A box or sample-entry kind as bounded, printable text."""
    return _measured_value(kind.decode("latin-1"))


def derivation_container(data: bytes) -> dict:
    """What the first bytes of the story derivation say about Meta's container
    and audio lines: the top-level boxes up to `moov` or `mdat`, whether `moov`
    comes first and where it ends, and, once the bytes hold `moov` whole, its
    edit lists, its sample entries and each audio track's codec, rate and
    channel count. A fact the bytes do not reach is None."""
    facts = {
        "boxes": [],
        "moov_first": None,
        "moov_end": None,
        "edit_lists": None,
        "sample_codecs": None,
        "audio": None,
    }
    start = 0
    while start + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1:
            if start + 16 > len(data):
                break
            size, header = struct.unpack_from(">Q", data, start + 8)[0], 16
        facts["boxes"].append(_fourcc(kind))
        if kind == b"mdat":
            facts["moov_first"] = False
            break
        if kind == b"moov":
            facts["moov_first"] = True
            if size >= header:
                facts["moov_end"] = start + size
                if start + size <= len(data):
                    facts.update(_moov_facts(data, start + header, start + size))
            break
        if size < header:
            # Size 0 runs to the end of the file, and a smaller one is no box:
            # no later box can be found.
            break
        start += size
    facts["boxes"] = _measured_value(facts["boxes"])
    return facts


def _moov_facts(data: bytes, start: int, end: int) -> dict:
    """`moov`'s edit lists, sample entries and audio tracks, read with the
    source walker's framing and budget (`_boxes`). A `moov` that does not nest
    within its bounds, or runs past the budget, is left unread, and says so."""
    budget = _BoxBudget(ValidationLimits())
    tracks = []
    try:
        for kind, payload, finish in _boxes(data, start, end, 1, budget):
            if kind == b"trak":
                tracks.append({"handler": None, "edit_lists": 0, "entries": []})
                _walk_track(data, payload, finish, 2, budget, tracks[-1])
        audio = [
            _sound(data, *entry, budget)
            for track in tracks
            if track["handler"] == b"soun"
            for entry in track["entries"]
        ]
    except (UnsupportedMedia, ValidationPending):
        return {"unread": "moov_unwalkable"}
    return {
        "edit_lists": sum(track["edit_lists"] for track in tracks),
        "sample_codecs": _measured_value(
            [_fourcc(entry[0]) for track in tracks for entry in track["entries"]]
        ),
        "audio": audio,
    }


def _walk_track(data, start, end, depth, budget, track) -> None:
    """A `trak`'s boxes into *track*: its handler type (`mdia`'s, which comes
    before a MOV `minf`'s data handler), its edit lists and its sample
    entries."""
    for kind, payload, finish in _boxes(data, start, end, depth, budget):
        if kind in (b"mdia", b"minf", b"stbl", b"edts"):
            _walk_track(data, payload, finish, depth + 1, budget, track)
        elif kind == b"elst":
            track["edit_lists"] += 1
        elif kind == b"hdlr" and track["handler"] is None and finish - payload >= 12:
            # A full box's version and flags and `pre_defined`, then the type.
            track["handler"] = data[payload + 8 : payload + 12]
        elif kind == b"stsd" and finish - payload >= 8:
            # A full box's version and flags and the entry count, then the
            # entries.
            track["entries"].extend(
                _boxes(data, payload + 8, finish, depth + 1, budget)
            )


def _sound(data, entry, content, finish, budget) -> dict:
    """An audio entry's codec, rate and channel count. An AudioSampleEntry
    holds 8 bytes of SampleEntry, a version and 6 reserved bytes, the channel
    count and 6 more, the 16.16 rate at +24 and its boxes at +28. An MP4 muxer
    may write that channel count as its template 2 whatever the stream carries
    (ffmpeg does), and a 16.16 rate cannot hold 88.2 kHz, so for AAC both come
    from its configuration; otherwise the rate is the entry's and the count is
    not read."""
    codec = _fourcc(entry)
    fixed = finish - content >= 28
    rate = struct.unpack_from(">I", data, content + 24)[0] >> 16 if fixed else None
    channels = None
    if (
        codec == _AAC_SAMPLE_ENTRY
        and fixed
        and data[content + 8 : content + 10] == bytes(2)
    ):
        for kind, payload, end in _boxes(data, content + 28, finish, 0, budget):
            if kind == b"esds":
                # A full box's version and flags, then its descriptors.
                configured, channels = _aac_config(data, payload + 4, end)
                rate = configured or rate
    return {"codec": codec, "channels": channels, "hz": rate}


def _aac_config(data: bytes, start: int, end: int) -> tuple:
    """The rate and channel count an `esds`'s AudioSpecificConfig names,
    between *start* and *end* (ISO/IEC 14496-1 and -3): through the ES
    descriptor and its decoder config to the decoder-specific info. Either is
    None when a descriptor is missing or cut short, or the configuration
    leaves it to a later element."""

    def descriptor(pos, limit, tag):
        # A tag, then a size of up to four bytes of seven bits each.
        if pos >= limit or data[pos] != tag:
            return None
        pos, size = pos + 1, 0
        for _ in range(4):
            if pos >= limit:
                return None
            size, pos = size << 7 | data[pos] & 0x7F, pos + 1
            if not data[pos - 1] & 0x80:
                break
        return (pos, pos + size) if pos + size <= limit else None

    found = descriptor(start, end, 0x03)
    if found is None or found[0] + 3 > found[1]:
        return None, None
    pos, limit = found
    # ES_ID, then the flags: a dependency's id, a URL, an OCR stream's id.
    flags = data[pos + 2]
    pos += 3 + (2 if flags & 0x80 else 0)
    if flags & 0x40:
        pos += 1 + (data[pos] if pos < limit else 0)
    pos += 2 if flags & 0x20 else 0
    found = descriptor(pos, limit, 0x04)
    # Thirteen bytes, objectTypeIndication to avgBitrate, then the
    # decoder-specific info.
    found = found and descriptor(found[0] + 13, found[1], 0x05)
    if not found:
        return None, None
    bits = "".join(
        f"{byte:08b}" for byte in data[found[0] : min(found[1], found[0] + 8)]
    )
    at = 0

    def field(width):
        nonlocal at
        value, at = bits[at : at + width], at + width
        return int(value, 2) if len(value) == width else None

    # The object type (31 escapes to six more bits), the rate's index (15 is
    # an explicit rate), then the channel configuration.
    if field(5) == 31:
        field(6)
    index = field(4)
    if index == 15:
        rate = field(24)
    else:
        rate = (
            _AAC_RATES[index] if index is not None and index < len(_AAC_RATES) else None
        )
    return rate, _AAC_CHANNELS.get(field(4))


def derivation_limits(media_kind: str) -> dict:
    """The matrix's limits a story derivation of *media_kind* is held to
    (`judge_derivation`): its size and, for a video, its audio's."""
    matrix = SUPPORTED_MEDIA_MATRIX
    if media_kind != "video":
        return {"bytes": matrix["provider_image_bytes"]}
    return {
        "bytes": matrix["provider_video_bytes"],
        "audio_codec": matrix["audio_codec"],
        "audio_hz": matrix["audio_hz"],
        "audio_channels": matrix["audio_channels"],
    }


def judge_derivation(observation: Mapping | None, media_kind: str) -> None:
    """Raise `UnsupportedMedia` for a story derivation that breaks a line of
    Meta's the matrix holds, from what the transit store's readiness probe
    read of it (`TransitStore.ready`): its size, and for a video its audio's
    codec and rate, then the lines behind the `REFUSE_*` switches. What the
    probe did not read is left to Meta's answer to the container call."""
    if not observation:
        return
    limits = derivation_limits(media_kind)
    size = observation.get("length")
    if isinstance(size, int) and size > limits["bytes"]:
        raise _measured(
            UnsupportedMedia("derivation_bytes"), bytes=size, limit=limits["bytes"]
        )
    container = observation.get("container") or {}
    if REFUSE_MOOV_POSITION and container.get("moov_first") is False:
        raise _measured(
            UnsupportedMedia("derivation_moov_position"), boxes=container.get("boxes")
        )
    if REFUSE_EDIT_LISTS and container.get("edit_lists"):
        raise _measured(
            UnsupportedMedia("derivation_edit_list"),
            edit_lists=container["edit_lists"],
        )
    for audio in container.get("audio") or ():
        if audio.get("codec") != _AAC_SAMPLE_ENTRY:
            raise _measured(
                UnsupportedMedia("derivation_audio_codec"), codec=audio.get("codec")
            )
        channels, hz = audio.get("channels"), audio.get("hz")
        if (
            REFUSE_AUDIO_CHANNELS
            and isinstance(channels, int)
            and channels > limits["audio_channels"]
        ):
            raise _measured(
                UnsupportedMedia("derivation_audio_channels"),
                channels=channels,
                limit=limits["audio_channels"],
            )
        if isinstance(hz, int) and hz > limits["audio_hz"]:
            raise _measured(
                UnsupportedMedia("derivation_audio_rate"),
                hz=hz,
                limit=limits["audio_hz"],
            )
