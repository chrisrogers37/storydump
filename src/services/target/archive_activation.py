"""Operator-issued archive activation evidence and the retained key ring.

Only trusted composition loads these, and nothing here is an estate census. The
Instagram archive is inactive unless one operator record binds THIS release and
the mix conversion run, AND the HMAC key ring loads whole. Anything less is
a named reason (`REASONS`), never a partial arming; no record, or a record that
fails any check, keeps every archive seam off.

The record's five evidence strings are required to be present and are never
stored, returned or logged: code cannot verify the external facts they attest,
and they may carry private references. `capability` is the one place in the
tree that builds an enabled `ArchiveCapability`
(`tests/src/services/target/test_archive_activation.py` pins that over the
whole tree). Composition roots call `resolve` once and log
`Activation.log_line`; nothing here logs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Mapping

from src.services.target.instagram_catalog import TombstoneKeys
from src.services.target.instagram_sources import ArchiveCapability, ArchiveRefused
from src.services.target.mix_conversion import MixConversionEvidence

RECORD_PATH = "INSTAGRAM_ARCHIVE_ACTIVATION_RECORD"
KEYS_VAR = "ARCHIVE_HMAC_KEYS"

# Why a process is inactive: the closed vocabulary of the compose log line, in
# the order the checks run (the record first, then the key ring).
NO_RECORD = "no_record"
RECORD_INVALID = "record_invalid"
RELEASE_MISMATCH = "release_mismatch"
CONVERSION_MISSING = "conversion_missing"
CONVERSION_MISMATCH = "conversion_mismatch"
KEYS_MISSING = "keys_missing"
KEYS_INVALID = "keys_invalid"
REASONS = (
    NO_RECORD,
    RECORD_INVALID,
    RELEASE_MISMATCH,
    CONVERSION_MISSING,
    CONVERSION_MISMATCH,
    KEYS_MISSING,
    KEYS_INVALID,
)

RECORD_MAX_BYTES = 65536
EVIDENCE_MAX_CHARS = 2048
# `video_validation` is the locked gate: image-only activation is not a record.
# `callback_identity` attests that Meta's callback `user_id` is the `/me` `id` the
# identity mapping stores: the premise deauthorize and deletion match on.
EVIDENCE_KEYS = (
    "meta_access",
    "graph_version",
    "video_validation",
    "deployment_drain",
    "callback_identity",
)

_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"[a-zA-Z0-9_.-]{1,128}")
# `<version>:<base64url>[=]`. The version is a positive decimal that fits the
# INTEGER `key_version` column; the alphabet is the URL-safe one alone (the
# decoder's own leniency would drop stray characters and accept `+` and `/`).
_KEY_ENTRY = re.compile(r"([1-9][0-9]{0,8}):([A-Za-z0-9_-]+)(={0,2})")


@dataclass(frozen=True)
class ArchiveActivationEvidence:
    """What the record proved, and no more: ids and hashes, never the evidence."""

    run_id: str
    digest: str
    api_sha: str
    worker_sha: str
    rollback_shas: tuple[str, ...]


@dataclass(frozen=True)
class Activation:
    """One compose's decision. `reason` is None exactly when armed, and the
    evidence is carried only then, so a present `evidence` always has its
    capability. The keys load on their own (receipts need them either way)."""

    capability: ArchiveCapability | None
    evidence: ArchiveActivationEvidence | None
    keys: TombstoneKeys | None = field(repr=False)
    reason: str | None

    @property
    def log_line(self) -> str:
        """The one line a compose logs: a reason or a run id, never a value."""
        if self.capability is None:
            return f"instagram archive: inactive ({self.reason})"
        return f"instagram archive: armed (run={self.evidence.run_id})"


def _release(value) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("sha"), str)
        and _SHA.fullmatch(value["sha"]) is not None
    )


def _evidence_text(value) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value.strip())
        and len(value) <= EVIDENCE_MAX_CHARS
    )


def _well_formed(value) -> bool:
    return (
        isinstance(value, dict)
        and type(value.get("v")) is int
        and value["v"] == 1
        and value.get("enabled") is True
        and isinstance(value.get("run_id"), str)
        and _RUN_ID.fullmatch(value["run_id"]) is not None
        and _release(value.get("api"))
        and _release(value.get("worker"))
        and isinstance(value.get("conversion_run_digest"), str)
        and _DIGEST.fullmatch(value["conversion_run_digest"]) is not None
        and isinstance(value.get("evidence"), dict)
        and all(_evidence_text(value["evidence"].get(k)) for k in EVIDENCE_KEYS)
        and isinstance(value.get("rollback_releases"), list)
        and bool(value["rollback_releases"])
        and all(_release(v) for v in value["rollback_releases"])
    )


def _read_evidence(path, *, service, release_sha, conversion):
    """(evidence, None) or (None, reason). Never raises, never echoes a value."""
    if not isinstance(path, str) or not path.strip():
        return None, NO_RECORD
    try:
        with open(path, "rb") as handle:
            raw = handle.read(RECORD_MAX_BYTES + 1)
    except (OSError, ValueError):
        return None, RECORD_INVALID
    if len(raw) > RECORD_MAX_BYTES:
        return None, RECORD_INVALID
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None, RECORD_INVALID
    if not _well_formed(value):
        return None, RECORD_INVALID
    # An unknown service has no release to bind to.
    if service not in ("api", "worker") or value[service]["sha"] != release_sha:
        return None, RELEASE_MISMATCH
    if not isinstance(conversion, MixConversionEvidence):
        return None, CONVERSION_MISSING
    if value["conversion_run_digest"] != conversion.digest:
        return None, CONVERSION_MISMATCH
    return (
        ArchiveActivationEvidence(
            value["run_id"],
            hashlib.sha256(raw).hexdigest(),
            value["api"]["sha"],
            value["worker"]["sha"],
            tuple(v["sha"] for v in value["rollback_releases"]),
        ),
        None,
    )


def _key_bytes(body: str, padding: str) -> bytes | None:
    missing = -len(body) % 4
    if padding and len(padding) != missing:
        return None
    try:
        return base64.urlsafe_b64decode(body + "=" * missing)
    except ValueError:  # binascii.Error, for a length no encoder produces
        return None


def _read_keys(env):
    """(ring, None) or (None, reason): one bad entry refuses the whole ring."""
    raw = env.get(KEYS_VAR)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, KEYS_MISSING
    if not isinstance(raw, str):
        return None, KEYS_INVALID
    ring: dict[int, bytes] = {}
    for entry in raw.split(","):
        found = _KEY_ENTRY.fullmatch(entry.strip(" \t\r\n"))
        if found is None:
            return None, KEYS_INVALID
        version, key = int(found[1]), _key_bytes(found[2], found[3])
        if key is None or version in ring:
            return None, KEYS_INVALID
        ring[version] = key
    try:
        # TombstoneKeys owns the key floor (at least 32 bytes each).
        return TombstoneKeys(current_version=max(ring), keys=ring), None
    except ArchiveRefused:
        return None, KEYS_INVALID


def load_evidence(
    path: str | None,
    *,
    service: str,
    release_sha: str | None,
    conversion: MixConversionEvidence | None,
) -> ArchiveActivationEvidence | None:
    """Trusted immutable evidence, or None. The record must bind this service's
    release and the conversion run in hand; `resolve` says which check refused."""
    return _read_evidence(
        path, service=service, release_sha=release_sha, conversion=conversion
    )[0]


def load_keys(env: Mapping[str, str]) -> TombstoneKeys | None:
    """The whole `"1:<b64url>,2:..."` ring, the highest version current, or
    None. Never a partial ring: one malformed entry refuses all of it."""
    return _read_keys(env)[0]


def capability(
    evidence: ArchiveActivationEvidence | None, keys: TombstoneKeys | None
) -> ArchiveCapability | None:
    """The tree's only enabled capability: evidence AND a key ring, else None."""
    if isinstance(evidence, ArchiveActivationEvidence) and isinstance(
        keys, TombstoneKeys
    ):
        return ArchiveCapability(enabled=True)
    return None


def resolve(
    env: Mapping[str, str],
    *,
    service: str,
    release_sha: str | None,
    conversion: MixConversionEvidence | None,
) -> Activation:
    """Read the record and the ring once and decide. Reasons keep the order the
    checks run in: an evidence reason shadows a key reason."""
    evidence, evidence_reason = _read_evidence(
        env.get(RECORD_PATH),
        service=service,
        release_sha=release_sha,
        conversion=conversion,
    )
    keys, keys_reason = _read_keys(env)
    armed = capability(evidence, keys)
    if armed is None:
        return Activation(None, None, keys, evidence_reason or keys_reason)
    return Activation(armed, evidence, keys, None)
