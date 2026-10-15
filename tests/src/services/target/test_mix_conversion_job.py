"""Actual converter composition and transaction ordering without database fixtures."""

from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import sys

import pytest

from src.services.target import jobs, mix_conversion, scheduler, versioned_mix
from src.services.target.work_loop import Parked, WorkerDeps, build_registry


def trusted():
    return mix_conversion.MixConversionEvidence(
        "synthetic-run", "d" * 64, "a" * 40, "b" * 40, ("c" * 40,)
    )


async def test_disabled_discovery_never_touches_executor():
    assert await mix_conversion.mint_jobs(object(), evidence=None) == 0


async def test_trusted_discovery_uses_one_bounded_real_door():
    seen = []

    class Executor:
        async def execute(self, sql, params):
            seen.append((str(sql), params))

            class Result:
                def scalar_one(self):
                    return 1

            return Result()

    e = trusted()
    assert await mix_conversion.mint_jobs(Executor(), evidence=e) == 1
    assert len(seen) == 1 and "fn_mix_conversion_jobs" in seen[0][0]
    assert seen[0][1] == {"limit": 1, "run_id": e.run_id, "digest": e.digest}


def test_converter_registry_parks_without_operator_record():
    assert isinstance(build_registry(WorkerDeps())["convert_source_mix"], Parked)


def test_converter_registry_is_callable_with_typed_record():
    deps = WorkerDeps(mix_conversion_evidence=trusted())
    assert callable(build_registry(deps)["convert_source_mix"])


async def test_converter_checks_actual_lease_after_whole_conversion(monkeypatch):
    events = []
    e = trusted()
    executor = object()
    job = {
        "id": "job",
        "lease_token": "lease",
        "workspace_id": "workspace",
        "payload": {
            "v": 1,
            "run_id": e.run_id,
            "run_digest": e.digest,
            "protocol_version": 2,
        },
    }

    async def convert(actual, *, workspace_id, evidence):
        assert actual is executor and workspace_id == job["workspace_id"]
        assert evidence is e
        events.append("conversion")

    async def lease(actual, job_id, token):
        assert actual is executor and (job_id, token) == ("job", "lease")
        events.append("lease")

    monkeypatch.setattr(versioned_mix, "convert_workspace", convert)
    monkeypatch.setattr(jobs, "assert_lease", lease)
    assert await mix_conversion.convert_job(executor, job, evidence=e) is None
    assert events == ["conversion", "lease"]


async def test_superseded_job_never_calls_converter(monkeypatch):
    events = []
    e = trusted()
    job = {
        "id": "job",
        "lease_token": "lease",
        "workspace_id": "workspace",
        "payload": {
            "v": 1,
            "run_id": "old-run",
            "run_digest": "e" * 64,
            "protocol_version": 2,
        },
    }

    class Executor:
        async def execute(self, sql, params):
            events.append("reason")
            assert params["reason"] == "mix_conversion_run_changed"

    async def convert(*args, **kwargs):
        pytest.fail("stale run must not convert")

    async def lock(*args, **kwargs):
        events.append("prefix")

    async def lease(*args):
        events.append("lease")

    async def finalize(*args):
        assert args[-1] == "failed"
        events.append("failed")

    monkeypatch.setattr(versioned_mix, "convert_workspace", convert)
    monkeypatch.setattr(mix_conversion.source_state, "lock_all_workspace_sources", lock)
    monkeypatch.setattr(jobs, "assert_lease", lease)
    monkeypatch.setattr(jobs, "finalize_job", finalize)
    assert (
        await mix_conversion.convert_job(Executor(), job, evidence=e)
        is jobs.SELF_FINALIZED
    )
    assert events == ["prefix", "lease", "reason", "failed"]


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_elected_clock_commits_ordinary_tick_before_conversion_discovery(
    monkeypatch, caplog, fail, exhausted
):
    events = []
    calls = 0

    @asynccontextmanager
    async def factory():
        nonlocal calls
        calls += 1
        n = calls
        events.append(f"enter{n}")

        class Session:
            async def commit(self):
                events.append(f"commit{n}")

        try:
            yield Session()
        finally:
            events.append(f"exit{n}")

    async def tick(*args, **kwargs):
        assert kwargs["max_inserts"] == 2
        events.append("ordinary")
        return {"o_slot_jobs": 2 if exhausted else 1}

    async def mint(*args, **kwargs):
        events.append("mint")
        if fail:
            raise RuntimeError("synthetic discovery failure")
        return 1

    class Election:
        async def try_acquire(self):
            return True

    monkeypatch.setattr(scheduler, "tick", tick)
    monkeypatch.setattr(mix_conversion, "mint_jobs", mint, raising=False)
    clock = scheduler.Clock(
        object(),
        factory,
        interval_seconds=1,
        max_inserts=2,
        refresh_cadence_seconds=1,
        recurring={"v": 1},
        conversion_evidence=trusted(),
    )
    clock._election = Election()
    counts = await clock.tick_once()
    assert events[:4] == ["enter1", "ordinary", "commit1", "exit1"]
    assert events[4:] == (
        []
        if exhausted
        else (
            ["enter2", "mint", "exit2"]
            if fail
            else ["enter2", "mint", "commit2", "exit2"]
        )
    )
    assert counts["conversion_jobs"] == (0 if fail or exhausted else 1)
    assert clock.inserts == (2 if exhausted else (1 if fail else 2))
    assert clock.consecutive_failures == int(fail and not exhausted)
    if fail and not exhausted:
        assert "ordinary tick committed" in caplog.text
        assert "NO JOBS WERE MINTED" not in caplog.text


@pytest.mark.parametrize("first", ["versioned_mix", "category_mix", "workspaces"])
def test_mix_public_exceptions_survive_fresh_process_import_order(first):
    root = Path(__file__).resolve().parents[4]
    script = (
        "import importlib;"
        f"importlib.import_module('src.services.target.{first}');"
        "from src.services.target import category_mix,versioned_mix;"
        "assert category_mix.MixInvalid is versioned_mix.MixInvalid;"
        "assert category_mix.MixConflict is versioned_mix.MixConflict;"
        "assert category_mix.convert_workspace is versioned_mix.convert_workspace;"
        "e=category_mix.MixInvalid('bad_ratio','synthetic');"
        "assert e.reason=='bad_ratio';"
        "assert str(e)=='mix invalid: bad_ratio — synthetic'"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(root),
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
