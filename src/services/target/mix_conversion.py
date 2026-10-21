"""The source-mix conversion run: every release has one, and converts.

There is no operator record. Each process takes its release's run
(`release_evidence`): the run id and digest its conversion jobs are keyed to,
so a deploy mints fresh jobs for any workspace an earlier one left unconverted.
Every release since the cutover speaks both mix protocols, so no older process
can be fed a converted mix it cannot read.
"""

from __future__ import annotations
from dataclasses import dataclass
import hashlib
import re

from sqlalchemy import text
from src.services.target import jobs, source_state

PROTOCOL_MAX = 2

#: A release is the 40-hex commit Railway sets on a Git deploy.
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class MixConversionEvidence:
    """The run a release's conversion jobs carry: an id and a digest."""

    run_id: str
    digest: str


def release_run(release_sha) -> tuple[str, str]:
    """`(run_id, digest)` for *release_sha*: one run per deployed commit.

    The shapes are the ones `fn_mix_conversion_jobs` and
    `fn_archive_reconcile_jobs` accept. A process with no usable commit (a
    local run, a test) is `release-local`."""
    sha = release_sha.strip().lower() if isinstance(release_sha, str) else ""
    if _SHA.fullmatch(sha) is None:
        sha = ""
    run_id = f"release-{sha[:12]}" if sha else "release-local"
    digest = hashlib.sha256(f"storydump-release:{sha or 'local'}".encode()).hexdigest()
    return run_id, digest


def release_evidence(release_sha) -> MixConversionEvidence:
    """This release's conversion run."""
    return MixConversionEvidence(*release_run(release_sha))


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
