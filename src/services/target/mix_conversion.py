"""Operator-issued conversion evidence; parsing is not an estate census.

Only trusted composition loads this durable run record. Archive activation is
separate. No record, or incomplete evidence, keeps conversion disabled.
"""

from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from sqlalchemy import text
from src.services.target import jobs, source_state

PROTOCOL_MAX = 2
RECORD_PATH = "SOURCE_MIX_CONVERSION_RECORD"


@dataclass(frozen=True)
class MixConversionEvidence:
    run_id: str
    digest: str
    api_sha: str
    worker_sha: str
    rollback_shas: tuple[str, ...]


def _release(value):
    return (
        isinstance(value, dict)
        and isinstance(value.get("sha"), str)
        and re.fullmatch(r"[0-9a-f]{40}", value["sha"]) is not None
        and type(value.get("source_mix_protocol_max")) is int
        and value["source_mix_protocol_max"] == PROTOCOL_MAX
        and value.get("expanded_schema") is True
        and value.get("both_protocols") is True
    )


def load_evidence(path: str | None, *, service: str, release_sha: str | None):
    """Return trusted immutable evidence or disabled. Never log private references."""
    if not path or service not in ("api", "worker"):
        return None
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 65536:
            return None
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or type(value.get("v")) is not int
            or value["v"] != 1
            or value.get("enabled") is not True
            or not isinstance(value.get("run_id"), str)
            or re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", value["run_id"]) is None
            or not _release(value.get("api"))
            or not _release(value.get("worker"))
            or value[service]["sha"] != release_sha
            or value.get("lease_disposition") not in ("exhausted", "fenced")
            or not all(
                isinstance(value.get(k), str) and 0 < len(value[k]) <= 2048
                for k in ("termination_evidence", "leases_evidence")
            )
            or not isinstance(value.get("rollback_releases"), list)
            or not value["rollback_releases"]
            or not all(_release(v) for v in value["rollback_releases"])
        ):
            return None
        return MixConversionEvidence(
            value["run_id"],
            hashlib.sha256(raw).hexdigest(),
            value["api"]["sha"],
            value["worker"]["sha"],
            tuple(v["sha"] for v in value["rollback_releases"]),
        )
    except (OSError, ValueError, TypeError):
        return None


def capability(env, service, evidence=None):
    sha = env.get("RAILWAY_GIT_COMMIT_SHA")
    return {
        "source_mix_protocol_max": PROTOCOL_MAX,
        "release_sha": sha,
        "conversion_enabled": isinstance(evidence, MixConversionEvidence),
    }


async def mint_jobs(executor, *, evidence=None):
    """Trusted elected-clock discovery; one tenant job per committed turn."""
    if not isinstance(evidence, MixConversionEvidence):
        return 0
    result = await executor.execute(
        text(
            "SELECT fn_mix_conversion_jobs(CAST(:limit AS int),"
            "CAST(:run_id AS text),CAST(:digest AS text))"
        ),
        {"limit": 1, "run_id": evidence.run_id, "digest": evidence.digest},
    )
    return int(result.scalar_one())


def _matches(payload, evidence):
    return (
        isinstance(payload, dict)
        and type(payload.get("v")) is int
        and payload["v"] == 1
        and type(payload.get("protocol_version")) is int
        and payload["protocol_version"] == PROTOCOL_MAX
        and payload.get("run_id") == evidence.run_id
        and payload.get("run_digest") == evidence.digest
    )


async def convert_job(executor, job, *, evidence=None):
    """One tenant conversion and its actual lease remain one transaction."""
    from src.services.target import versioned_mix

    if not isinstance(evidence, MixConversionEvidence):
        raise versioned_mix.MixConversionUnavailable()
    if not _matches(job.get("payload"), evidence):
        await source_state.lock_all_workspace_sources(
            executor, workspace_id=str(job["workspace_id"])
        )
        await jobs.assert_lease(executor, job["id"], job["lease_token"])
        await executor.execute(
            text(
                "UPDATE jobs SET payload=payload||jsonb_build_object("
                "'conversion_refusal',CAST(:reason AS text)) WHERE id=:id"
            ),
            {"id": job["id"], "reason": "mix_conversion_run_changed"},
        )
        await jobs.finalize_job(executor, job["id"], job["lease_token"], "failed")
        return jobs.SELF_FINALIZED
    await versioned_mix.convert_workspace(
        executor, workspace_id=str(job["workspace_id"]), evidence=evidence
    )
    # The loop's plain-executor finalization shares this outer transaction.
    await jobs.assert_lease(executor, job["id"], job["lease_token"])
