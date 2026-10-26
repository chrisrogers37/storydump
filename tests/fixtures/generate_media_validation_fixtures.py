"""Reproducible matrix stimuli, and the judgment the validator gives each.

Run: python generate_media_validation_fixtures.py /temporary/output-directory
No provider calls or application startup. Flags are stimuli, not acceptance proof.
"""

import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageCms


def box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def generate(directory):
    directory.mkdir(parents=True, exist_ok=True)
    commands = []
    cases = []

    def record(name, kind, state, reason):
        data = (directory / name).read_bytes()
        cases.append(
            {
                "name": name,
                "kind": kind,
                "state": state,
                "reason": reason,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )

    def image(name, fmt="JPEG", **kwargs):
        Image.new("RGB", (360, 640), (40, 80, 160)).save(
            directory / name, format=fmt, **kwargs
        )

    def ffmpeg(name, args):
        argv = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            *args,
            str(directory / name),
        ]
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=90, check=False
        )
        commands.append({"argv": argv, "exit": result.returncode})
        (directory / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        if result.returncode:
            raise RuntimeError("fixture generation failed")

    image("untagged.jpg")
    record("untagged.jpg", "image", "supported", "validated")
    profile = bytearray(
        ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    )
    profile[24:36] = struct.pack(">6H", 2026, 1, 1, 0, 0, 0)
    profile[84:100] = bytes(16)
    image("srgb.jpg", icc_profile=bytes(profile))
    record("srgb.jpg", "image", "supported", "validated")
    image("malformed-profile.jpg", icc_profile=b"not an ICC profile")
    record(
        "malformed-profile.jpg", "image", "validation_pending", "color_space_unproven"
    )
    image("png-named-jpeg.jpg", "PNG")
    record("png-named-jpeg.jpg", "image", "unsupported", "image_format")
    raw = (directory / "srgb.jpg").read_bytes()
    (directory / "truncated.jpg").write_bytes(raw[:-100])
    record("truncated.jpg", "image", "unsupported", "malformed_image")
    for cap in (7_999_999, 8_000_000, 8_000_001):
        remaining = cap - len(raw)
        chunks = []
        while remaining:
            amount = min(65_537, remaining)
            if 0 < remaining - amount < 4:
                amount -= 4 - (remaining - amount)
            chunks.append(
                b"\xff\xfe" + struct.pack(">H", amount - 2) + bytes(amount - 4)
            )
            remaining -= amount
        name = f"bytes-{cap}.jpg"
        (directory / name).write_bytes(raw[:2] + b"".join(chunks) + raw[2:])
        record(
            name,
            "image",
            "unsupported" if cap > 8_000_000 else "supported",
            "byte_limit" if cap > 8_000_000 else "validated",
        )

    def video(
        name,
        *,
        count=90,
        fps=30,
        size="160x160",
        extra=(),
        codec="libx264",
        pix="yuv420p",
        reason="validated",
        state="supported",
    ):
        encoder = ["-c:v", codec, "-threads", "1", "-pix_fmt", pix]
        if codec == "libx264":
            encoder += [
                "-x264-params",
                "open-gop=0:keyint=30:min-keyint=30:scenecut=0:nal-hrd=vbr",
                "-b:v",
                "500k",
                "-maxrate",
                "1000k",
                "-bufsize",
                "2000k",
            ]
        elif codec == "libx265":
            encoder += [
                "-x265-params",
                "pools=none:frame-threads=1:wpp=0:open-gop=0:keyint=30:min-keyint=30:scenecut=0:bitrate=500:vbv-maxrate=1000:vbv-bufsize=2000:hrd=1",
                "-tag:v",
                "hvc1",
            ]
        ffmpeg(
            name,
            [
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={size}:rate={fps}",
                "-frames:v",
                str(count),
                "-an",
                *encoder,
                *extra,
                "-map_metadata",
                "-1",
                "-use_editlist",
                "0",
                "-movflags",
                "+faststart",
            ],
        )
        record(name, "video", state, reason)

    video("h264.mp4")
    video("hevc.mp4", codec="libx265")
    video("format.mov")
    for count in (89, 90, 1800, 1801):
        refused = count == 89
        video(
            f"frames-{count}.mp4",
            count=count,
            reason="duration" if refused else "validated",
            state="unsupported" if refused else "supported",
        )
    for fps in (22, 23, 60, 61):
        refused = fps in (22, 61)
        video(
            f"fps-{fps}.mp4",
            fps=fps,
            count=fps * 3,
            reason="fps" if refused else "validated",
            state="unsupported" if refused else "supported",
        )
    # The stream's frame size, chroma, interlacing and GOP are the story
    # derivation's to set, so none of them is judged.
    for size in (
        "1920x1080",
        "1922x1080",
        "100x1000",
        "98x1000",
        "1000x100",
        "1002x100",
    ):
        video(f"geometry-{size}.mp4", size=size)
    video("chroma422.mp4", pix="yuv422p")
    video(
        "interlaced.mp4",
        extra=("-flags", "+ilme+ildct", "-x264-params", "tff=1:open-gop=0"),
    )
    video(
        "open-gop.mp4",
        count=180,
        extra=(
            "-x264-params",
            "open-gop=1:keyint=30:min-keyint=30:scenecut=0:bframes=3",
        ),
    )
    video("mpeg4.mp4", codec="mpeg4", state="unsupported", reason="video_codec")
    base = directory / "h264.mp4"
    for name, opts, state, reason in (
        ("late-moov.mp4", (), "supported", "validated"),
        (
            "edits.mp4",
            ("-movflags", "+faststart", "-use_editlist", "1"),
            "supported",
            "validated",
        ),
        (
            "fragmented.mp4",
            ("-movflags", "+empty_moov+frag_keyframe"),
            "validation_pending",
            "container_evidence_incomplete",
        ),
    ):
        ffmpeg(
            name,
            ["-i", str(base), "-map", "0", "-c", "copy", "-use_editlist", "0", *opts],
        )
        record(name, "video", state, reason)
    for rate, channels in ((44100, 1), (48000, 2), (96000, 2), (48000, 3)):
        name = f"audio-{rate}-{channels}.mp4"
        ffmpeg(
            name,
            [
                "-i",
                str(base),
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=1000:sample_rate={rate}:duration=3",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                str(rate),
                "-ac",
                str(channels),
                "-use_editlist",
                "0",
                "-movflags",
                "+faststart",
            ],
        )
        # Audio is the story derivation's to set, so none of these is judged.
        record(name, "video", "supported", "validated")
    raw = base.read_bytes()
    (directory / "truncated.mp4").write_bytes(raw[:-100])
    record("truncated.mp4", "video", "unsupported", "malformed_container")
    for cap in (39_999_999, 40_000_000, 40_000_001):
        name = f"bytes-{cap}.mp4"
        (directory / name).write_bytes(raw + box(b"free", bytes(cap - len(raw) - 8)))
        record(
            name,
            "video",
            "unsupported" if cap > 40_000_000 else "supported",
            "byte_limit" if cap > 40_000_000 else "validated",
        )
    (directory / "fake-elst.mp4").write_bytes(raw + box(b"free", b"elst"))
    record("fake-elst.mp4", "video", "supported", "validated")
    entry = box(b"url ", bytes(4) + b"../must-not-open\0")
    dref = box(b"dref", bytes(4) + struct.pack(">I", 1) + entry)
    (directory / "external-dref.mp4").write_bytes(
        box(b"moov", box(b"trak", box(b"mdia", box(b"minf", box(b"dinf", dref)))))
    )
    record("external-dref.mp4", "video", "unsupported", "external_reference")
    # Pixel budgets are application bounds, not invented Story image geometry.
    for height, state, reason in (
        (2000, "supported", "validated"),
        (2001, "validation_pending", "pixel_budget"),
    ):
        name = f"pixels-4000x{height}.jpg"
        Image.new("RGB", (4000, height), (40, 80, 160)).save(directory / name)
        record(name, "image", state, reason)
    video("chroma420-10bit.mp4", pix="yuv420p10le")
    video(
        "vfr-ambiguous.mp4",
        count=180,
        extra=(
            "-vf",
            "settb=1/120,setpts='if(lt(N,90),N*4,360+(N-90))'",
            "-enc_time_base",
            "1/120",
            "-fps_mode",
            "passthrough",
        ),
    )
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "packet=pos,size",
            "-of",
            "json",
            str(base),
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )
    packet = json.loads(probe.stdout)["packets"][60]
    corrupt = bytearray(raw)
    start, size = int(packet["pos"]), int(packet["size"])
    corrupt[start : start + size] = bytes(size)
    (directory / "malformed-second-gop.mp4").write_bytes(corrupt)
    # The probe reads the stream's start; a packet broken further in is the
    # derivation's to meet.
    record("malformed-second-gop.mp4", "video", "supported", "validated")
    (directory / "manifest.json").write_text(json.dumps(cases, indent=2) + "\n")
    return cases


if __name__ == "__main__":
    generate(Path(sys.argv[1]))
