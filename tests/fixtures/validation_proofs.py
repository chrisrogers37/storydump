"""Independent digest contract and pre-#73 evidence for a long video fixture."""

from dataclasses import replace
import hashlib
import json


def documented_validation_digest(kind, proof, evidence):
    document = {
        "kind": kind,
        "generation": proof.source_generation,
        "revision": proof.observation_revision,
        "asset": proof.selected_asset_id,
        "matrix": proof.matrix_version,
        "evidence_version": proof.evidence_version,
        "state": proof.state,
        "reason": proof.reason,
        "evidence": evidence,
    }
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def legacy_video_validation(proof):
    """The evidence and fingerprint the pre-#73 writer recorded for a 61 s clip."""
    assert proof.state == "supported" and proof.evidence["duration_seconds"] == "61"
    evidence = {
        **proof.evidence,
        "derivation": {
            "video_codec": ["h264", "high", "4.2"],
            "audio_codec": "aac",
            "audio_hz": 48_000,
            "fps": [23, 60],
            "video_bits_per_second": 13_000_000,
            "longest_seconds": 60,
        },
        "trimmed_to_seconds": "60",
    }
    return replace(
        proof,
        evidence=evidence,
        fingerprint=documented_validation_digest("video", proof, evidence),
    )
