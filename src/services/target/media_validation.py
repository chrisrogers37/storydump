"""Bounded local media evidence, independent of source admission and SQL.

Matrix source: Meta IG User /media.
https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media
FFmpeg interface: ffprobe's JSON stream and format entries.

Resource limits/private cwd are NOT a filesystem sandbox. MOV external-track
references the container walker reads are refused; the probe runs with dref and
path options disabled, which is what holds one in a container the walker could
not read whole. No network, transformations, captions, URLs or raw decoder
diagnostics are retained.

Meta never receives a video's source file. It fetches the transit store's
story derivation of it (`transit.story_transformation`, delivered as MP4 in the
story frame), so a video is judged only on what the derivation keeps from it:
its size against what the transit store takes, a well-formed self-contained
container, a video stream, its duration and its frame rate.
What only the file could settle never leaves it pending: a container the walker
cannot read whole, a stream the probe does not decode, more streams than the
probe opens and a duration nothing states are left to the derivation, which
re-encodes the file, and recorded. The
derivation is trimmed to the longest story and encoded to the matrix's story
video (`story_video_encoding`), so a longer video is kept and only a shorter one
is refused for its length, the container's or its longest stream's. The frame
rate is still refused outside the matrix:
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
from contextlib import contextmanager
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from .vocabulary import PUBLISH_MAX_BYTES

MATRIX_VERSION = "instagram-story-2026-10-26-v5"
EVIDENCE_VERSION = 4
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
        # What the transit store takes. Meta's own size limits govern the
        # story derivation it delivers, and are judged on it
        # (`judge_derivation`).
        "byte_caps": MappingProxyType(dict(PUBLISH_MAX_BYTES)),
        "image": ("JPEG", "sRGB; provider converts other color spaces"),
        "video": (
            "MOV/MP4, well formed and self-contained",
            "a video stream, which the derivation encodes to H.264 with AAC audio",
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


def _bounds(check: str, kind: str) -> dict:
    """The bounds *check* holds a *kind* of file to, as the matrix states them:
    recorded beside what the check measured, so a record says what it was
    judged against without anyone reading the matrix again."""
    if check == "byte_limit":
        return {"cap": SUPPORTED_MEDIA_MATRIX["byte_caps"][kind]}
    if check == "duration":
        shortest, longest = SUPPORTED_MEDIA_MATRIX["duration_seconds"]
        return {"min_seconds": shortest, "max_seconds": longest}
    if check == "fps":
        lowest, highest = SUPPORTED_MEDIA_MATRIX["fps"]
        return {"min_fps": lowest, "max_fps": highest}
    return {}


def _check_record(check: str, kind: str, measured: Mapping[str, object]) -> Mapping:
    """One check's record, `{"check", "measured"}`: what it measured and the
    bounds it holds a *kind* of file to (`_bounds`). The shape of a refusal's
    `failed_check` and of each of a supported result's `passed_checks`."""
    return MappingProxyType(
        {"check": check, "measured": {**measured, **_bounds(check, kind)}}
    )


def _passed(kind: str, evidence: Mapping[str, object]) -> tuple:
    """Each check a supported file passed that holds it to a bound, recorded
    from the evidence. A video whose probe states no duration or no frame rate
    was not judged on it."""
    checks = [_check_record("byte_limit", kind, {"bytes": evidence["bytes"]})]
    if kind == "video":
        if _positive_fraction(evidence["duration_seconds"]) is not None:
            duration = {"duration_seconds": evidence["duration_seconds"]}
            if evidence["trimmed_to_seconds"] is not None:
                duration["trimmed_to_seconds"] = evidence["trimmed_to_seconds"]
            checks.append(_check_record("duration", kind, duration))
        if _positive_fraction(evidence["fps"]) is not None:
            checks.append(_check_record("fps", kind, {"fps": evidence["fps"]}))
    return tuple(checks)


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
    #: The most streams the probe opens (`-max_streams`). A file with more is
    #: not probed: its story derivation takes the first video and audio stream.
    streams: int = 16
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
            "streams": 16,
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
    #: values it measured, with the bounds it held them to (`{"check",
    #: "measured"}`). Not part of the fingerprint.
    failed_check: Mapping[str, object] | None = None
    #: For a supported result: each check it passed that holds a file to a
    #: bound, in `failed_check`'s shape (`_check_record`). Not part of the
    #: fingerprint.
    passed_checks: tuple[Mapping[str, object], ...] = ()


class ValidationPending(Exception):
    """Closed safe diagnostic, never the raw tool exception."""


#: Every check a file can be refused by: the closed set `UnsupportedMedia`
#: carries. The refusal sentences (`refusal_evidence`) cover it, and the checks
#: it no longer holds (`refusal_evidence.LEGACY_CHECKS`).
UNSUPPORTED_CHECKS = frozenset(
    {
        "byte_limit",
        "derivation_audio_channels",
        "derivation_audio_codec",
        "derivation_audio_rate",
        "derivation_bytes",
        "derivation_edit_list",
        "derivation_moov_position",
        "duration",
        "external_reference",
        "fps",
        "image_format",
        "malformed_container",
        "malformed_image",
        "malformed_video",
        "stream_topology",
    }
)


class UnsupportedMedia(Exception):
    """A file refused by one check, named from `UNSUPPORTED_CHECKS`."""

    def __init__(self, check: str):
        if check not in UNSUPPORTED_CHECKS:
            raise ValueError(f"not a check in UNSUPPORTED_CHECKS: {check!r}")
        super().__init__(check)


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
    exc.measured = _bounded(values)
    return exc


def _bounded(values: Mapping[str, object]) -> dict:
    """*values* as a record keeps measured values (`_measured_value`)."""
    return {key: _measured_value(value) for key, value in values.items()}


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
    #: For a refusal, and for a walk that stopped short: the box list and the
    #: sample codecs walked so far.
    measured: Mapping[str, object] = field(default_factory=dict)
    #: Why a walk that refused nothing could not read the container whole
    #: (`container_budget`, `container_evidence_incomplete`), or None.
    stopped: str | None = None
    #: The tracks the walk found, each a stream to the probe: for a walk that
    #: stopped short, those it reached.
    streams: int = 0
    #: The sample entries it found that the probe does not decode
    #: (`_PROBE_SAMPLE_ENTRIES`), which the story derivation re-encodes.
    undecoded: tuple[str, ...] = ()


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
    """Small bounded ISO-BMFF walker; opaque payload text never becomes a box.

    A container the walk cannot read whole, past its budget
    (`container_budget`) or holding what it does not follow
    (`container_evidence_incomplete`: a fragmented or compressed movie, a
    reference of a kind it does not know), is not refused for that: the story
    derivation re-encodes the file. The walk keeps what it read and names where
    it stopped."""
    budget = _BoxBudget(limits or ValidationLimits())
    top = []
    tracks = []
    codecs = []
    # A compressed or fragmented movie, which the walk does not follow.
    unfollowed = False

    def walk(start, end, depth, track=None):
        nonlocal unfollowed
        for kind, payload, finish in _boxes(data, start, end, depth, budget):
            if depth == 0:
                top.append(kind)
            if kind in (b"cmov", b"moof", b"mvex"):
                unfollowed = True
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

    def stop():
        """Why the walk could not read the container whole, or None."""
        try:
            walk(0, len(data), 0)
        except ValidationPending as exc:
            return str(exc)
        unresolved = any(
            not track["indices"]
            or not track["refs"]
            or any(
                not 0 < index <= len(track["refs"]) or not track["refs"][index - 1]
                for index in track["indices"]
            )
            for track in tracks
        )
        if (
            top.count(b"moov") != 1
            or b"mdat" not in top
            or not tracks
            or unfollowed
            or unresolved
        ):
            return "container_evidence_incomplete"
        return None

    def walked():
        """The top-level boxes, the count of boxes read and the sample codecs
        walked so far, bounded as any measured value."""
        return _bounded(
            {
                "boxes": [kind.decode("ascii", errors="replace") for kind in top],
                "box_count": budget.count,
                "sample_codecs": codecs,
            }
        )

    try:
        stopped = stop()
    except UnsupportedMedia as exc:
        return ContainerEvidence(str(exc), measured=walked())
    return ContainerEvidence(
        None,
        measured=walked() if stopped else {},
        stopped=stopped,
        streams=len(tracks),
        undecoded=tuple(c for c in codecs if c not in _PROBE_SAMPLE_ENTRIES),
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


@contextmanager
def _private_copy(data: bytes, limits: ValidationLimits):
    """*data* as a read-only file in a private temporary directory, removed on
    exit: the bounded tools read this copy, never the caller's bytes, and run
    in its directory."""
    with tempfile.TemporaryDirectory(
        prefix="media-validation-", dir=limits.temporary_root
    ) as directory:
        local = Path(directory) / "asset"
        local.write_bytes(data)
        local.chmod(0o400)
        yield local


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
            evidence.update(await _validate_video(source.data, limits, deadline))
        else:
            with _private_copy(source.data, limits) as local:
                command = [
                    sys.executable,
                    "-c",
                    "from src.services.target.media_validation import _image_worker;import sys;_image_worker(sys.argv[1],int(sys.argv[2]))",
                    str(local),
                    str(limits.pixels),
                ]
                output = await run_bounded(command, limits, local.parent, deadline)
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
    except (UnsupportedMedia, ValidationPending) as exc:
        state = (
            "unsupported" if isinstance(exc, UnsupportedMedia) else "validation_pending"
        )
        reason = str(exc)
        failed_check = _check_record(reason, source.kind, getattr(exc, "measured", {}))
        passed_checks = ()
    else:
        failed_check = None
        passed_checks = _passed(source.kind, evidence)
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
        passed_checks=passed_checks,
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
    as the derivation takes it), its duration (the longest the container or any
    stream states) and its frame rate. A video shorter than the shortest story
    is refused; one longer than the longest is kept, trimmed by the derivation,
    and recorded as trimmed. A duration or a frame rate the probe does not
    state is not judged; a frame rate outside the matrix is refused."""
    streams = facts.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    if not videos:
        raise _measured(
            UnsupportedMedia("stream_topology"), streams=len(streams), video_streams=0
        )
    video = videos[0]
    stated = [
        _positive_fraction(entry.get("duration"))
        for entry in (facts.get("format", {}), *streams)
    ]
    duration = max((value for value in stated if value is not None), default=None)
    fps = _positive_fraction(video.get("avg_frame_rate"))
    timing = dict(duration_seconds=duration, fps=fps)
    shortest = SUPPORTED_MEDIA_MATRIX["duration_seconds"][0]
    if duration is not None and duration < shortest:
        raise _measured(UnsupportedMedia("duration"), **timing)
    slowest, fastest = SUPPORTED_MEDIA_MATRIX["fps"]
    if fps is not None and not slowest <= fps <= fastest:
        raise _measured(UnsupportedMedia("fps"), **timing)
    audio = any(s.get("codec_type") == "audio" for s in streams)
    return _video_evidence(video, duration, fps, audio)


def _video_evidence(video, duration=None, fps=None, audio=None) -> dict:
    """A video's evidence of what the probe read: its video stream's codec and
    frame size, its duration and frame rate ("unstated" for one it did not
    state) and whether it holds audio, with the encoding its derivation is
    sent, which trims it to the longest story."""
    encoding = story_video_encoding()
    longest = encoding["longest_seconds"]
    return {
        "codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "duration_seconds": "unstated" if duration is None else str(duration),
        "fps": "unstated" if fps is None else str(fps),
        "audio": audio,
        "trimmed_to_seconds": (
            str(longest) if duration is not None and duration > longest else None
        ),
        "derivation": encoding,
    }


#: The decoders the probe may open on untrusted bytes, with the sample entries
#: that name them. A stream outside them is not decoded: the probe reads its
#: container facts, and the story derivation re-encodes it.
_PROBE_DECODERS = {"h264": ("avc1", "avc3"), "hevc": ("hvc1", "hev1"), "aac": ("mp4a",)}
_PROBE_CODECS = ",".join(_PROBE_DECODERS)  # `-codec_whitelist`
_PROBE_SAMPLE_ENTRIES = tuple(
    e for entries in _PROBE_DECODERS.values() for e in entries
)


def _closed_decoder(line: bytes) -> bool:
    """Whether a lowercased probe stderr line is ffmpeg declining a decoder outside
    `-codec_whitelist`: the probe's own bound, not a fault of the file, whose
    container facts still stand."""
    return b"codec (" in line and b"not on whitelist" in line


#: ffmpeg's line for a message logged again at once ("Last message repeated
#: N times"): it adds nothing to the line it repeats.
_REPEATED = b"last message repeated"


class _ClosedDecoder(Exception):
    """The probe stopped at a decoder `-codec_whitelist` keeps closed, and at
    nothing else (`_closed_decoder`): ffprobe ends when it cannot open one."""


def _probe_succeeded(output):
    """Return when the probe ran clean, apart from the decoders it may not
    open (`_closed_decoder`), and raise `_ClosedDecoder` when one of them is
    all that stopped it. Words that establish malformed bytes refuse the file;
    a resource or capability failure leaves it pending."""
    if output.returncode < 0:
        raise ValidationPending("resource_budget")
    stderr = output.stderr.lower()
    closed = False
    if b"not on whitelist" in stderr:
        lines = [line for line in stderr.splitlines() if _REPEATED not in line]
        kept = [line for line in lines if not _closed_decoder(line)]
        closed, stderr = len(kept) < len(lines), b"\n".join(kept)
    if not stderr:
        if output.returncode == 0:
            return
        if closed:
            raise _ClosedDecoder()
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


async def _validate_video(data, limits, deadline):
    """A video's evidence: the container walk, then what the probe reads of a
    private copy. A walk that stopped short is recorded with what it read
    (`container`), and so are the sample entries the probe does not decode
    (`undecoded_codecs`). A file with more streams than the probe opens is not
    probed, and one whose probe stops at a decoder it keeps closed is judged
    without it: either way the file is its story derivation's to read, its
    evidence says why (`probe_skipped`), and each fact the probe would give is
    unstated."""
    container = inspect_container(data, limits)
    if container.reason:
        raise _measured(UnsupportedMedia(container.reason), **container.measured)
    evidence = {}
    if container.stopped:
        evidence["container"] = {"stopped": container.stopped, **container.measured}
    if container.undecoded:
        evidence["undecoded_codecs"] = _measured_value(container.undecoded)
    if container.streams > limits.streams:
        evidence["probe_skipped"] = {
            "streams": container.streams,
            "max_streams": limits.streams,
        }
        facts = _video_evidence({})
    else:
        try:
            with _private_copy(data, limits) as local:
                facts = await _probe_video(local, limits, deadline)
        except _ClosedDecoder:
            evidence["probe_skipped"] = {
                "closed_decoders": _measured_value(container.undecoded)
            }
            facts = _video_evidence({})
    return {**evidence, **facts}


async def _probe_video(local, limits, deadline):
    """What the probe reads of the private copy at *local*, run in its
    directory."""
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
            str(limits.streams),
            "-max_pixels",
            str(limits.pixels),
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,duration",
            "-of",
            "json",
            str(local),
        ],
        limits,
        local.parent,
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


def judge_derivation(observation: Mapping | None, media_kind: str) -> None:
    """Raise `UnsupportedMedia` for a story derivation that breaks a line of
    Meta's the matrix holds, from what the transit store's readiness probe
    read of it (`TransitStore.ready`): its size, and for a video its audio's
    codec and rate, then the lines behind the `REFUSE_*` switches. What the
    probe did not read is left to Meta's answer to the container call."""
    if not observation:
        return
    matrix = SUPPORTED_MEDIA_MATRIX
    limit = matrix[
        "provider_video_bytes" if media_kind == "video" else "provider_image_bytes"
    ]
    size = observation.get("length")
    if isinstance(size, int) and size > limit:
        raise _measured(UnsupportedMedia("derivation_bytes"), bytes=size, limit=limit)
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
            and channels > matrix["audio_channels"]
        ):
            raise _measured(
                UnsupportedMedia("derivation_audio_channels"),
                channels=channels,
                limit=matrix["audio_channels"],
            )
        if isinstance(hz, int) and hz > matrix["audio_hz"]:
            raise _measured(
                UnsupportedMedia("derivation_audio_rate"),
                hz=hz,
                limit=matrix["audio_hz"],
            )
