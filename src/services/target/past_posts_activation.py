"""The past-posts source's activation: on in every process that holds the key ring.

There is no operator record and no switch. Instagram past posts arms wherever
the `PAST_POSTS_HMAC_KEYS` ring loads whole, because the ring keys the tombstones
and deletion receipts the past-posts source cannot run without; a missing or malformed
ring is a named reason (`REASONS`) and nothing arms. An armed process carries
its release's run (`release_evidence`, the same run as the mix conversion's),
which keys its reconciliation jobs, so each deploy retries what an earlier one
could not finish. `capability` is the one place in the tree that builds an
enabled `PastPostsCapability` (`tests/src/services/target/test_past_posts_activation.py`
pins that over the whole tree). Composition roots call `resolve` once and log
`Activation.log_line`; nothing here logs, and no key is ever echoed.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Mapping

from src.services.target.instagram_catalog import TombstoneKeys
from src.services.target.instagram_sources import PastPostsCapability, PastPostsRefused
from src.services.target.mix_conversion import release_run

KEYS_VAR = "PAST_POSTS_HMAC_KEYS"

# Why a process is inactive: the closed vocabulary of the compose log line.
KEYS_MISSING = "keys_missing"
KEYS_INVALID = "keys_invalid"
REASONS = (KEYS_MISSING, KEYS_INVALID)

# `<version>:<base64url>[=]`. The version is a positive decimal that fits the
# INTEGER `key_version` column; the alphabet is the URL-safe one alone (the
# decoder's own leniency would drop stray characters and accept `+` and `/`).
_KEY_ENTRY = re.compile(r"([1-9][0-9]{0,8}):([A-Za-z0-9_-]+)(={0,2})")


@dataclass(frozen=True)
class PastPostsActivationEvidence:
    """The run an armed process keys its reconciliation jobs to: its release's."""

    run_id: str
    digest: str


def release_evidence(release_sha) -> PastPostsActivationEvidence:
    """This release's past-posts run (`mix_conversion.release_run`)."""
    return PastPostsActivationEvidence(*release_run(release_sha))


@dataclass(frozen=True)
class Activation:
    """One compose's decision. `reason` is None exactly when armed, and the
    run is carried only then, so a present `evidence` always has its
    capability."""

    capability: PastPostsCapability | None
    evidence: PastPostsActivationEvidence | None
    keys: TombstoneKeys | None = field(repr=False)
    reason: str | None

    @property
    def log_line(self) -> str:
        """The one line a compose logs: a reason or a run id, never a value."""
        if self.capability is None:
            return f"instagram past posts: off ({self.reason})"
        return f"instagram past posts: on (run={self.evidence.run_id})"


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
    except PastPostsRefused:
        return None, KEYS_INVALID


def load_keys(env: Mapping[str, str]) -> TombstoneKeys | None:
    """The whole `"1:<b64url>,2:..."` ring, the highest version current, or
    None. Never a partial ring: one malformed entry refuses all of it."""
    return _read_keys(env)[0]


def capability(
    evidence: PastPostsActivationEvidence | None, keys: TombstoneKeys | None
) -> PastPostsCapability | None:
    """The tree's only enabled capability: a release run AND a key ring, else None."""
    if isinstance(evidence, PastPostsActivationEvidence) and isinstance(
        keys, TombstoneKeys
    ):
        return PastPostsCapability(enabled=True)
    return None


def resolve(env: Mapping[str, str], *, release_sha=None) -> Activation:
    """Read the ring once and decide: armed with this release's run, or the
    reason the ring refused."""
    keys, reason = _read_keys(env)
    if keys is None:
        return Activation(None, None, None, reason)
    evidence = release_evidence(release_sha)
    return Activation(capability(evidence, keys), evidence, keys, None)
