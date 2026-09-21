"""The environment verbs: ``health``, ``deploys`` and ``doctor``.

``health`` reads the API's three health surfaces and exits 0 only when every
one reports well (4 otherwise — "API unreachable or failed", spec §6 — with
the report still printed). ``deploys`` shells out to the ``railway`` binary
through one seam: it checks the login and the linked project before reading
a deployment, and a missing binary, a logged-out account or another project
each exit 5 with a plain sentence; ``--watch`` ends 0 when both services'
latest deploys are SUCCESS and 6 when one FAILED or CRASHED. The Railway
answers are a version-stamped fixture of the real JSON. ``doctor`` reports
each check as ok / wrong / missing / skipped with a one-line fix and exits
with the first non-ok check's code, in report order.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from src.services.target.vocabulary import (
    EXIT_API_UNREACHABLE,
    EXIT_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_RAILWAY_UNREACHABLE,
    EXIT_USAGE,
    EXIT_WATCH_FAILED,
    check_envelope,
)
from storydump_cli import railway
from storydump_cli.config import Config, write_config
from tests.storydump_cli.test_main import PERSON, Api, one_envelope, run, runtime

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "railway_4.30.3.json").read_text()
)
NOW = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)

HEALTH = {
    "status": "ok",
    "version": "2.1.0",
    "uptime_seconds": 120,
    "target_database": True,
    "db_role": "svc_ingress",
    "pool": {"size": 10, "checked_out": 1, "checked_out_peak": 3},
    "ingress_workers": 1,
    # `TapMetrics.snapshot()`'s real shape: two scalars and the per-outcome
    # map nested under `taps`. It used to be spelled the renderer's way
    # (`executed`/`replayed` at the top level), which is why nothing
    # caught the blank cells (#1360).
    "taps": {
        "taps_total": 13,
        "taps": {"executed": 12, "older_card": 1},
        "answer_failed": 0,
    },
    "webhook": {"ok": True, "bot": "storydump_app_bot", "url": "https://api/x"},
    "webhook_live": {
        "at": "2026-09-15T14:59:30+00:00",
        "url": "https://api/x",
        "pending_update_count": 0,
        "last_error_date": None,
        "last_error_message": None,
        "max_connections": 40,
        "allowed_updates": ["message", "callback_query"],
    },
}
#: The real shapes (`src/api/app.py`'s three routes): the two dependency
#: surfaces carry aggregates, never a `status` — `health` hands them to the
#: fleet monitors' own `classify`, whose strictness these fixtures satisfy.
SCHEDULING = {
    "stalled": 0,
    "accounts_active": 2,
    "max_lag_seconds": 12,
    "worker": {
        "succeeded_ever": 1,
        "last_success_age_seconds": 30,
        "overdue_ready": 0,
        "max_overdue_seconds": None,
    },
    "backpressure": {"lanes": {"interactive": {"ready": 0}, "bulk": {"ready": 2}}},
}
POSTING = {
    "posted_ever": 13,
    "last_post_age_seconds": 3600,
    "intents_ever": 40,
    "oldest_intent_age_seconds": 120,
    "debited_total": 13,
    "ledger_days": 4,
    "accounts_active": 1,
    "oldest_active_destination_age_seconds": 86400,
}
POSTURE = {
    "ledger": "present",
    "migrations": [{"version": v, "status": "applied"} for v in range(1, 78)],
    "role": {"user": "svc_ingress", "bypassrls": False},
    "rls": [],
    "doors": [],
}


def health_api(over=None) -> Api:
    routes = {
        ("GET", "/health"): (200, HEALTH),
        ("GET", "/health/scheduling"): (200, SCHEDULING),
        ("GET", "/health/posting"): (200, POSTING),
        ("GET", "/api/v1/me/principal"): (200, PERSON),
        ("GET", "/api/v1/ops/posture"): (
            200,
            {"v": 1, "kind": "posture", "data": POSTURE, "error": None},
        ),
    }
    routes.update(over or {})
    return Api(routes)


class ScriptedRailway:
    """``railway <args>`` → ``(code, stdout, stderr)``; a list of answers for
    one argv is consumed in order (a watch); an exception is raised."""

    def __init__(self, answers):
        self.answers = {
            tuple(k): (list(v) if isinstance(v, list) else v)
            for k, v in answers.items()
        }
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args):
        argv = tuple(args)
        self.calls.append(argv)
        answer = self.answers.get(argv)
        if answer is None:
            raise AssertionError(f"unscripted railway call: {argv}")
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


LIST_WORKER = (
    "deployment",
    "list",
    "--service",
    "worker",
    "--environment",
    "production",
    "--json",
)
LIST_API = (
    "deployment",
    "list",
    "--service",
    "storydump",
    "--environment",
    "production",
    "--json",
)
NEWEST = FIXTURE["deployments"]["worker"][0]
NEWEST_COMMIT = NEWEST["meta"]["commitHash"][:7]


def ok_json(value) -> tuple[int, str, str]:
    return (0, json.dumps(value) + "\n", "")


def railway_answers(over=None) -> dict:
    answers = {
        ("--version",): (0, "railway 4.30.3\n", ""),
        ("whoami",): (0, "Logged in as Christopher Rogers (c@example.test) 👋\n", ""),
        ("status", "--json"): ok_json(FIXTURE["status"]),
        LIST_WORKER: ok_json(FIXTURE["deployments"]["worker"]),
        LIST_API: ok_json(FIXTURE["deployments"]["storydump"]),
    }
    answers.update(over or {})
    return answers


def bounded_sleeper(record=None, limit=3):
    """A watch's sleeper that ends the watch (Ctrl-C) after *limit* sleeps, so
    a mutant that never reaches its end cannot hang the suite."""
    slept = [] if record is None else record

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) > limit:
            raise KeyboardInterrupt

    return sleep


def env_runtime(tmp_path, api: Api, rail=None, **kw):
    rt = runtime(tmp_path, api, **kw)
    rt.now_fn = lambda: NOW
    rt.run_process = rail if rail is not None else ScriptedRailway(railway_answers())
    return rt


# --- health -------------------------------------------------------------------


def test_health_reads_the_three_surfaces_and_is_ok(tmp_path):
    api = health_api()
    result = run(env_runtime(tmp_path, api), "health")
    assert result.exit_code == EXIT_OK, result.output
    assert api.paths("GET")[:3] == ["/health", "/health/scheduling", "/health/posting"]
    for word in (
        "health ok",
        "2.1.0",
        "svc_ingress",
        "storydump_app_bot",
        "healthy",
        "posting",
    ):
        assert word in result.stdout, (word, result.stdout)


def test_health_json_is_one_envelope_with_the_three_payloads(tmp_path):
    result = run(env_runtime(tmp_path, health_api()), "--json", "health")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "health"
    verdicts = document["data"]["verdicts"]
    assert document["data"] == {
        "ok": True,
        "api": HEALTH,
        "scheduling": SCHEDULING,
        "posting": POSTING,
        "verdicts": {
            "api": {"state": "ok", "detail": "/health says ok"},
            "scheduling": {
                "state": "healthy",
                "detail": verdicts["scheduling"]["detail"],
            },
            "posting": {"state": "posting", "detail": verdicts["posting"]["detail"]},
            "webhook": {
                "state": "registered",
                "detail": verdicts["webhook"]["detail"],
            },
        },
    }, "the monitors' own states ride the report; the webhook is judged too"


def test_health_exits_4_when_a_surface_is_not_well_but_still_prints(tmp_path):
    """A due system job unclaimed past the monitor's 900 s grace is its
    WORKER_DOWN verdict; the report is still the answer."""
    dead = {
        **SCHEDULING,
        "worker": {
            **SCHEDULING["worker"],
            "overdue_ready": 3,
            "max_overdue_seconds": 1200,
        },
    }
    api = health_api({("GET", "/health/scheduling"): (200, dead)})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    document = one_envelope(result)
    assert document["data"]["ok"] is False and document["error"] is None, (
        "the report is the answer; the exit code is the verdict"
    )
    assert document["data"]["scheduling"]["worker"]["overdue_ready"] == 3
    assert document["data"]["verdicts"]["scheduling"]["state"] == "worker-down"


def test_health_reads_a_due_job_inside_the_grace_as_well(tmp_path):
    """The monitor's grace, not a stricter rule of the CLI's own."""
    waiting = {
        **SCHEDULING,
        "worker": {
            **SCHEDULING["worker"],
            "overdue_ready": 1,
            "max_overdue_seconds": 30,
        },
    }
    api = health_api({("GET", "/health/scheduling"): (200, waiting)})
    assert run(env_runtime(tmp_path, api), "health").exit_code == EXIT_OK


def test_health_reads_a_stale_worker_as_not_well(tmp_path):
    stale = {
        **SCHEDULING,
        "worker": {**SCHEDULING["worker"], "last_success_age_seconds": 14 * 3600},
    }
    api = health_api({("GET", "/health/scheduling"): (200, stale)})
    assert run(env_runtime(tmp_path, api), "health").exit_code == EXIT_API_UNREACHABLE


def test_health_reads_a_first_post_overdue_past_its_grace_as_not_well(tmp_path):
    never = {
        **POSTING,
        "posted_ever": 0,
        "last_post_age_seconds": None,
        "oldest_intent_age_seconds": 4 * 86400,
    }
    api = health_api({("GET", "/health/posting"): (200, never)})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_API_UNREACHABLE
    assert (
        one_envelope(result)["data"]["verdicts"]["posting"]["state"]
        == "never-posted-overdue"
    )
    fresh = {
        **never,
        "oldest_intent_age_seconds": 3600,
        "oldest_active_destination_age_seconds": 3600,
    }
    api = health_api({("GET", "/health/posting"): (200, fresh)})
    assert run(env_runtime(tmp_path, api), "health").exit_code == EXIT_OK, (
        "inside the grace: not an outage"
    )


def test_health_reads_a_lag_past_the_stall_threshold_as_not_well(tmp_path):
    lagging = {**SCHEDULING, "stalled": 1, "max_lag_seconds": 1200}
    api = health_api({("GET", "/health/scheduling"): (200, lagging)})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_API_UNREACHABLE
    assert one_envelope(result)["data"]["verdicts"]["scheduling"]["state"] == "stalled"


def test_health_reads_a_mistyped_surface_as_not_well(tmp_path):
    """The monitors are strict: a missing key is unreachable, never healthy."""
    broken = {k: v for k, v in SCHEDULING.items() if k != "stalled"}
    api = health_api({("GET", "/health/scheduling"): (200, broken)})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_API_UNREACHABLE
    assert (
        one_envelope(result)["data"]["verdicts"]["scheduling"]["state"] == "unreachable"
    )


def test_health_reads_a_webhook_that_failed_to_register_as_not_well(tmp_path):
    """`/health.webhook` is the API's own registration report (a snapshot from
    startup): `ok: false` with the error is a bot nobody is delivering to."""
    broken = {**HEALTH, "webhook": {"ok": False, "error": "BotApiError"}}
    api = health_api({("GET", "/health"): (200, broken)})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    document = one_envelope(result)
    assert document["data"]["ok"] is False
    verdict = document["data"]["verdicts"]["webhook"]
    assert verdict["state"] == "unregistered" and "BotApiError" in verdict["detail"]
    # a registration the API chose to skip (not production, autoregister off)
    # is a fact in the report, not an outage
    skipped = {
        **HEALTH,
        "webhook": {
            "ok": False,
            "skipped": "autoregister off (not the production environment)",
        },
    }
    api = health_api({("GET", "/health"): (200, skipped)})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_OK, result.output
    # the live sample (healthy, in HEALTH) decides when there is one: registered
    assert one_envelope(result)["data"]["verdicts"]["webhook"]["state"] == "registered"


def test_health_reads_an_undelivered_backlog_as_not_well(tmp_path):
    """`/health.webhook_live` is what Telegram holds right now: a backlog with a
    last delivery error names our route failing — the signal the API samples
    every minute for exactly this reading (`_sample_webhook_live`)."""
    stuck = {
        **HEALTH,
        "webhook_live": {
            **HEALTH["webhook_live"],
            "pending_update_count": 4123,
            "last_error_date": 1758000000,
            "last_error_message": "Wrong response from the webhook: 500 Internal Server Error",
        },
    }
    api = health_api({("GET", "/health"): (200, stuck)})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    verdict = one_envelope(result)["data"]["verdicts"]["webhook"]
    assert verdict["state"] == "undelivered"
    assert "4123" in verdict["detail"] and "500" in verdict["detail"]
    # an old error with nothing pending is history, not an outage
    drained = {
        **HEALTH,
        "webhook_live": {
            **HEALTH["webhook_live"],
            "pending_update_count": 0,
            "last_error_date": 1758000000,
            "last_error_message": "Wrong response from the webhook: 500 Internal Server Error",
        },
    }
    api = health_api({("GET", "/health"): (200, drained)})
    assert run(env_runtime(tmp_path, api), "health").exit_code == EXIT_OK
    # an API too old to report the webhook at all is not judged on it
    silent = {k: v for k, v in HEALTH.items() if k not in ("webhook", "webhook_live")}
    api = health_api({("GET", "/health"): (200, silent)})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_OK, result.output
    assert one_envelope(result)["data"]["verdicts"]["webhook"]["state"] == "unsampled"


def test_a_skipped_registration_with_a_live_backlog_is_still_undelivered(tmp_path):
    """Autoregister off in production (`TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER=0`,
    an operator driving the bot by hand) still has a bot to deliver to: with a
    live sample the sample decides, and a backlog behind an error is not well.
    Without a live sample, skipped is a fact, not an outage."""
    skipped = {"ok": False, "skipped": "autoregister switched off"}
    stuck = {
        **HEALTH["webhook_live"],
        "pending_update_count": 4123,
        "last_error_message": "Wrong response from the webhook: 500 Internal Server Error",
    }
    api = health_api(
        {
            ("GET", "/health"): (
                200,
                {**HEALTH, "webhook": skipped, "webhook_live": stuck},
            )
        }
    )
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    assert one_envelope(result)["data"]["verdicts"]["webhook"]["state"] == "undelivered"
    quiet = {k: v for k, v in HEALTH.items() if k != "webhook_live"}
    api = health_api({("GET", "/health"): (200, {**quiet, "webhook": skipped})})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_OK, result.output
    assert one_envelope(result)["data"]["verdicts"]["webhook"]["state"] == "skipped"


def test_health_reads_a_surface_that_is_not_an_object_as_not_well(tmp_path):
    """A 200 whose JSON is not the surface's object (a string, a list) is no
    answer — never well."""
    for payload in ("nope", [], 7):
        api = health_api({("GET", "/health/posting"): (200, payload)})
        result = run(env_runtime(tmp_path, api), "--json", "health")
        assert result.exit_code == EXIT_API_UNREACHABLE, (payload, result.output)
        assert (
            one_envelope(result)["data"]["verdicts"]["posting"]["state"]
            == "unreachable"
        )


def test_health_reads_a_silence_as_not_well_whatever_the_destinations(tmp_path):
    """Sixteen days without a post — the silence `/health/posting` exists for
    (the monitor's SILENT, 48 h). The monitor pages with or without an active
    destination (`posting_health.py` forbids that exemption by name), and so
    does the CLI."""
    silent = {**POSTING, "last_post_age_seconds": 16 * 86400}
    api = health_api({("GET", "/health/posting"): (200, silent)})
    assert run(env_runtime(tmp_path, api), "health").exit_code == EXIT_API_UNREACHABLE
    quiet = {
        **silent,
        "accounts_active": 0,
        "oldest_active_destination_age_seconds": None,
    }
    api = health_api({("GET", "/health/posting"): (200, quiet)})
    assert run(env_runtime(tmp_path, api), "health").exit_code == EXIT_API_UNREACHABLE


def test_health_reports_a_surface_that_answers_503_and_exits_4(tmp_path):
    api = health_api(
        {
            ("GET", "/health/scheduling"): (
                503,
                {"detail": "target database not configured"},
            )
        }
    )
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    document = one_envelope(result)
    assert document["data"]["api"]["status"] == "ok"
    assert "503" in document["data"]["scheduling"]["error"]
    assert document["data"]["posting"] == POSTING, (
        "the other surfaces are still reported"
    )


def test_health_reads_a_bad_api_status_as_not_well(tmp_path):
    api = health_api({("GET", "/health"): (200, {**HEALTH, "status": "degraded"})})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    assert one_envelope(result)["data"]["ok"] is False


def test_health_reads_a_quiet_estate_as_well(tmp_path):
    """No active destination and a live worker is the monitor's `no-signal`:
    nothing to watch is not an outage, and the CLI does not page on it."""
    quiet = {**SCHEDULING, "accounts_active": 0, "stalled": 0, "max_lag_seconds": None}
    api = health_api({("GET", "/health/scheduling"): (200, quiet)})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_OK, result.output
    assert (
        one_envelope(result)["data"]["verdicts"]["scheduling"]["state"] == "no-signal"
    )
    # no destination AND no system job ever finished: `worker-unknown`, a notice
    unknown = {
        **SCHEDULING,
        "accounts_active": 0,
        "stalled": 0,
        "max_lag_seconds": None,
        "worker": {
            "succeeded_ever": 0,
            "last_success_age_seconds": None,
            "overdue_ready": 0,
            "max_overdue_seconds": None,
        },
    }
    api = health_api({("GET", "/health/scheduling"): (200, unknown)})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_OK, result.output
    assert (
        one_envelope(result)["data"]["verdicts"]["scheduling"]["state"]
        == "worker-unknown"
    )


def test_health_reads_a_briefly_due_cursor_as_well(tmp_path):
    """`stalled` counts cursors that are due; the monitor pages on the LAG
    (`max_lag_seconds` past 600 s), because a briefly-due cursor is normal —
    and the CLI is the monitor's verdict, not a stricter one."""
    due = {**SCHEDULING, "stalled": 2, "max_lag_seconds": 12}
    api = health_api({("GET", "/health/scheduling"): (200, due)})
    assert run(env_runtime(tmp_path, api), "health").exit_code == EXIT_OK


def test_health_needs_no_token(tmp_path):
    api = health_api()
    result = run(env_runtime(tmp_path, api, token=None), "health")
    assert result.exit_code == EXIT_OK, result.output
    assert all("Authorization" not in r.headers for r in api.calls)


def test_an_api_that_does_not_answer_health_is_exit_4(tmp_path):
    api = health_api({("GET", "/health"): httpx.ConnectError("down")})
    result = run(env_runtime(tmp_path, api), "health")
    assert result.exit_code == EXIT_API_UNREACHABLE


# --- deploys ------------------------------------------------------------------


def test_deploys_lists_the_latest_deployment_per_service_with_its_commit(tmp_path):
    rail = ScriptedRailway(railway_answers())
    result = run(env_runtime(tmp_path, health_api(), rail), "deploys")
    assert result.exit_code == EXIT_OK, result.output
    assert rail.calls[:2] == [("whoami",), ("status", "--json")], (
        "the login and the linked project are checked before any deployment is read"
    )
    for word in ("worker", "storydump", "SUCCESS", NEWEST_COMMIT, "production"):
        assert word in result.stdout, (word, result.stdout)


def test_deploys_json_carries_the_railway_version_and_the_project(tmp_path):
    result = run(env_runtime(tmp_path, health_api()), "--json", "deploys")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "deploys"
    data = document["data"]
    assert (
        data["railway_version"] == FIXTURE["railway_version"] == railway.TESTED_VERSION
    )
    assert data["project"] == {"name": "storydump", "id": FIXTURE["status"]["id"]}
    assert data["environment"] == "production"
    assert [s["service"] for s in data["services"]] == ["storydump", "worker"]
    worker = next(s for s in data["services"] if s["service"] == "worker")
    assert worker["rows"][0] == {
        "id": NEWEST["id"],
        "service": "worker",
        "status": NEWEST["status"],
        "created_at": NEWEST["createdAt"],
        "commit": NEWEST_COMMIT,
        "commit_hash": NEWEST["meta"]["commitHash"],
        "branch": NEWEST["meta"]["branch"],
        "message": NEWEST["meta"]["commitMessage"].splitlines()[0],
    }, "the message is its first line; the commit its short hash"


def test_deploys_reads_the_production_environment_and_sorts_newest_first(tmp_path):
    """The binary defaults to the LINKED environment; the verb names
    production, and orders the rows itself."""
    reversed_rows = list(reversed(FIXTURE["deployments"]["worker"]))
    rail = ScriptedRailway(railway_answers({LIST_WORKER: ok_json(reversed_rows)}))
    result = run(env_runtime(tmp_path, health_api(), rail), "--json", "deploys")
    assert result.exit_code == EXIT_OK, result.output
    assert LIST_WORKER in rail.calls and LIST_API in rail.calls
    worker = next(
        s for s in one_envelope(result)["data"]["services"] if s["service"] == "worker"
    )
    assert worker["rows"][0]["id"] == NEWEST["id"], (
        "newest first whatever the binary's order"
    )


def test_a_hung_binary_is_exit_5(tmp_path):
    import subprocess

    rail = ScriptedRailway({("whoami",): subprocess.TimeoutExpired("railway", 60)})
    result = run(env_runtime(tmp_path, health_api(), rail), "deploys")
    assert result.exit_code == EXIT_RAILWAY_UNREACHABLE, result.output
    assert "did not answer" in result.stderr


def test_a_notice_before_the_json_is_skipped(tmp_path):
    noisy = (
        0,
        "A new version of railway is available\n" + json.dumps(FIXTURE["status"]),
        "",
    )
    rail = ScriptedRailway(railway_answers({("status", "--json"): noisy}))
    assert (
        run(env_runtime(tmp_path, health_api(), rail), "deploys").exit_code == EXIT_OK
    )


def test_deploys_reads_the_fixtures_version_of_the_binary():
    """The fixture is stamped with the `railway` version it was captured from;
    the parser is written for that version and says so."""
    assert FIXTURE["railway_version"] == railway.TESTED_VERSION
    assert railway.parse_version("railway 4.30.3\n") == "4.30.3"


def test_a_missing_railway_binary_is_exit_5_with_the_install_sentence(tmp_path):
    rail = ScriptedRailway({("whoami",): FileNotFoundError("railway")})
    result = run(env_runtime(tmp_path, health_api(), rail), "deploys")
    assert result.exit_code == EXIT_RAILWAY_UNREACHABLE, result.output
    assert "not installed" in result.stderr


def test_a_logged_out_railway_is_exit_5_naming_railway_login(tmp_path):
    rail = ScriptedRailway(
        railway_answers(
            {("whoami",): (1, "", "Unauthorized. Please login with `railway login`\n")}
        )
    )
    result = run(env_runtime(tmp_path, health_api(), rail), "deploys")
    assert result.exit_code == EXIT_RAILWAY_UNREACHABLE
    assert "railway login" in result.stderr
    assert ("status", "--json") not in rail.calls


def test_another_linked_project_is_exit_5_naming_it(tmp_path):
    other = {
        **FIXTURE["status"],
        "name": "artemis",
        "id": "00000000-0000-4000-8000-000000000000",
    }
    rail = ScriptedRailway(railway_answers({("status", "--json"): ok_json(other)}))
    result = run(env_runtime(tmp_path, health_api(), rail), "deploys")
    assert result.exit_code == EXIT_RAILWAY_UNREACHABLE
    assert "artemis" in result.stderr and "railway link" in result.stderr
    assert not any(c[:2] == ("deployment", "list") for c in rail.calls)


def test_no_linked_project_is_exit_5(tmp_path):
    rail = ScriptedRailway(
        railway_answers({("status", "--json"): (1, "", "No linked project found\n")})
    )
    result = run(env_runtime(tmp_path, health_api(), rail), "deploys")
    assert result.exit_code == EXIT_RAILWAY_UNREACHABLE
    assert "railway link" in result.stderr


def test_deploys_watch_ends_0_when_both_latest_deploys_succeed(tmp_path):
    building = [{**NEWEST, "status": "BUILDING"}]
    rail = ScriptedRailway(
        railway_answers(
            {
                LIST_WORKER: [
                    ok_json(building),
                    ok_json(FIXTURE["deployments"]["worker"]),
                ]
            }
        )
    )
    rt = env_runtime(tmp_path, health_api(), rail)
    slept: list[float] = []
    rt.sleep_fn = bounded_sleeper(slept)
    result = run(rt, "deploys", "--watch", "--every", "5")
    assert result.exit_code == EXIT_OK, result.output
    assert slept == [5.0]
    assert "BUILDING" in result.stdout and "SUCCESS" in result.stdout


@pytest.mark.parametrize("status", ["FAILED", "CRASHED"])
def test_deploys_watch_ends_6_when_a_latest_deploy_failed(tmp_path, status):
    failed = [{**FIXTURE["deployments"]["storydump"][0], "status": status}]
    rail = ScriptedRailway(railway_answers({LIST_API: ok_json(failed)}))
    rt = env_runtime(tmp_path, health_api(), rail)
    rt.sleep_fn = bounded_sleeper()
    result = run(rt, "deploys", "--watch")
    assert result.exit_code == EXIT_WATCH_FAILED, result.output
    assert status in result.stderr and "storydump" in result.stderr


def test_deploys_watch_with_a_commit_waits_for_that_commits_deploys(tmp_path):
    """Right after a push the previous SUCCESS rows are still the latest;
    with --commit the watch ends only on that commit's deployments."""
    new_worker = {
        **NEWEST,
        "id": "n1",
        "status": "SUCCESS",
        "createdAt": "2027-01-01T00:00:00Z",
    }
    new_worker["meta"] = {**NEWEST["meta"], "commitHash": "deadbeefcafe"}
    old_api = FIXTURE["deployments"]["storydump"][0]
    new_api = {
        **old_api,
        "id": "n2",
        "status": "SUCCESS",
        "createdAt": "2027-01-01T00:00:00Z",
    }
    new_api["meta"] = {**old_api["meta"], "commitHash": "deadbeefcafe"}
    rail = ScriptedRailway(
        railway_answers(
            {
                LIST_WORKER: [
                    ok_json(FIXTURE["deployments"]["worker"]),
                    ok_json([new_worker]),
                ],
                LIST_API: [
                    ok_json(FIXTURE["deployments"]["storydump"]),
                    ok_json([new_api]),
                ],
            }
        )
    )
    rt = env_runtime(tmp_path, health_api(), rail)
    slept: list[float] = []
    rt.sleep_fn = bounded_sleeper(slept)
    result = run(rt, "deploys", "--watch", "--commit", "deadbee")
    assert result.exit_code == EXIT_OK, result.output
    assert slept == [15.0], (
        "the first read (the previous commit's SUCCESS rows) did not end it"
    )


@pytest.mark.parametrize(
    "given",
    [
        NEWEST["meta"]["commitHash"],  # the full hash a push prints
        NEWEST["meta"]["commitHash"].upper(),
        NEWEST["meta"]["commitHash"][:12],
    ],
)
def test_deploys_watch_matches_a_commit_by_its_full_hash_too(tmp_path, given):
    """Railway lists the full hash; the CLI shows seven characters. A watch
    started with the hash a push printed (40 characters, any case) must match
    the row — a prefix comparison against the SHORT hash never does, and the
    watch would run until Ctrl-C."""
    rail = ScriptedRailway(railway_answers())
    rt = env_runtime(tmp_path, health_api(), rail)
    slept: list[float] = []
    rt.sleep_fn = bounded_sleeper(slept, limit=1)
    result = run(rt, "deploys", "--watch", "--commit", given)
    assert result.exit_code == EXIT_OK, result.output
    assert slept == [], (
        "the fixture's latest rows ARE that commit: done on the first read"
    )


def test_deploys_watch_reports_the_watched_commits_failure_under_a_full_hash(tmp_path):
    failed = [{**FIXTURE["deployments"]["storydump"][0], "status": "FAILED"}]
    rail = ScriptedRailway(railway_answers({LIST_API: ok_json(failed)}))
    rt = env_runtime(tmp_path, health_api(), rail)
    rt.sleep_fn = bounded_sleeper()
    full = FIXTURE["deployments"]["storydump"][0]["meta"]["commitHash"]
    result = run(rt, "deploys", "--watch", "--commit", full)
    assert result.exit_code == EXIT_WATCH_FAILED, result.output
    assert "FAILED" in result.stderr


def test_deploys_watch_reads_a_removed_deployment_as_failed(tmp_path):
    """A REMOVED latest deployment will never become SUCCESS: waiting on it is
    waiting forever."""
    removed = [{**FIXTURE["deployments"]["storydump"][0], "status": "REMOVED"}]
    rail = ScriptedRailway(railway_answers({LIST_API: ok_json(removed)}))
    rt = env_runtime(tmp_path, health_api(), rail)
    rt.sleep_fn = bounded_sleeper()
    result = run(rt, "deploys", "--watch")
    assert result.exit_code == EXIT_WATCH_FAILED, result.output
    assert "REMOVED" in result.stderr


def test_deploys_watch_waits_for_a_service_with_no_rows(tmp_path):
    """Done means BOTH services' latest rows are terminal — a service that has
    not deployed yet is not one that succeeded."""
    rail = ScriptedRailway(railway_answers({LIST_API: ok_json([])}))
    rt = env_runtime(tmp_path, health_api(), rail)
    slept: list[float] = []
    rt.sleep_fn = bounded_sleeper(slept, limit=2)
    result = run(rt, "deploys", "--watch", "--every", "5")
    assert result.exit_code == EXIT_OK, result.output
    assert slept == [5.0, 5.0, 5.0], "it kept waiting until Ctrl-C ended it"


def test_deploys_watch_ends_6_when_the_wait_exceeds_its_timeout(tmp_path):
    """An agent cannot press Ctrl-C: --timeout bounds the wait, and running
    out of it is the watch's failure (6), named."""
    building = [{**NEWEST, "status": "BUILDING"}]
    rail = ScriptedRailway(railway_answers({LIST_WORKER: ok_json(building)}))
    rt = env_runtime(tmp_path, health_api(), rail)
    ticks = iter(range(0, 10_000, 10))
    rt.now_fn = lambda: NOW + __import__("datetime").timedelta(seconds=next(ticks))
    rt.sleep_fn = bounded_sleeper(limit=50)
    result = run(rt, "--json", "deploys", "--watch", "--every", "5", "--timeout", "25")
    assert result.exit_code == EXIT_WATCH_FAILED, result.output
    documents = [json.loads(line) for line in result.stdout.splitlines()]
    assert documents[-1]["error"]["reason"] == "watch_failed"
    assert "25" in documents[-1]["error"]["detail"]


def test_deploys_watch_rides_out_a_railway_blip(tmp_path):
    """One `railway deployment list` answering 502 mid-watch is not the
    answer an unattended watch waits for: re-read, like an API 503."""
    building = [{**NEWEST, "status": "BUILDING"}]
    rail = ScriptedRailway(
        railway_answers(
            {
                LIST_WORKER: [
                    ok_json(building),
                    (1, "", "502 Bad Gateway\n"),
                    ok_json(FIXTURE["deployments"]["worker"]),
                ]
            }
        )
    )
    rt = env_runtime(tmp_path, health_api(), rail)
    slept: list[float] = []
    rt.sleep_fn = bounded_sleeper(slept, limit=5)
    result = run(rt, "deploys", "--watch", "--every", "5")
    assert result.exit_code == EXIT_OK, result.output
    assert slept == [5.0, 5.0], "it slept through the blip and read again"


def test_an_empty_commit_is_usage(tmp_path):
    rail = ScriptedRailway(railway_answers())
    rt = env_runtime(tmp_path, health_api(), rail)
    result = run(rt, "--json", "deploys", "--watch", "--commit", "  ")
    assert result.exit_code == EXIT_USAGE, result.output
    assert rail.calls == []


def test_deploys_timeout_without_watch_is_usage_before_any_railway_call(tmp_path):
    rail = ScriptedRailway(railway_answers())
    rt = env_runtime(tmp_path, health_api(), rail)
    result = run(rt, "--json", "deploys", "--timeout", "30")
    assert result.exit_code == EXIT_USAGE, result.output
    assert one_envelope(result)["error"]["reason"] == "usage"
    assert rail.calls == [], "refused before the login and the link were checked"


def test_health_reads_a_failed_webhook_sampler_as_not_well(tmp_path):
    """`webhook_live` carrying `error` is the API's own minute-sampler failing
    to ask Telegram (`_sample_webhook_live`): not the same as no report, and
    not well — the one reading that says our probe is blind."""
    blind = {
        **HEALTH,
        "webhook_live": {"at": "2026-09-15T14:59:30+00:00", "error": "ConnectError"},
    }
    api = health_api({("GET", "/health"): (200, blind)})
    result = run(env_runtime(tmp_path, api), "--json", "health")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    verdict = one_envelope(result)["data"]["verdicts"]["webhook"]
    assert verdict["state"] == "sampler_failed" and "ConnectError" in verdict["detail"]


def test_a_failed_deployment_list_is_exit_5_not_an_empty_service(tmp_path):
    rail = ScriptedRailway(
        railway_answers(
            {LIST_API: (1, "", "Unauthorized. Please login with `railway login`\n")}
        )
    )
    rt = env_runtime(tmp_path, health_api(), rail)
    result = run(rt, "--json", "deploys")
    assert result.exit_code == EXIT_RAILWAY_UNREACHABLE, result.output
    assert "storydump" in one_envelope(result)["error"]["detail"]


def test_deploys_watch_with_a_commit_ignores_another_commits_failure(tmp_path):
    old_failed = [{**FIXTURE["deployments"]["storydump"][0], "status": "FAILED"}]
    rail = ScriptedRailway(railway_answers({LIST_API: ok_json(old_failed)}))
    rt = env_runtime(tmp_path, health_api(), rail)
    rt.sleep_fn = bounded_sleeper()
    result = run(rt, "deploys", "--watch", "--commit", "deadbee")
    assert result.exit_code == EXIT_OK, "another commit's failure is not this watch's"


def test_deploys_watch_json_is_one_envelope_per_read(tmp_path):
    building = [{**NEWEST, "status": "BUILDING"}]
    rail = ScriptedRailway(
        railway_answers(
            {
                LIST_WORKER: [
                    ok_json(building),
                    ok_json(FIXTURE["deployments"]["worker"]),
                ]
            }
        )
    )
    rt = env_runtime(tmp_path, health_api(), rail)
    rt.sleep_fn = bounded_sleeper()
    result = run(rt, "--json", "deploys", "--watch")
    assert result.exit_code == EXIT_OK, result.output
    documents = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(documents) == 2
    for document in documents:
        check_envelope(document)
        assert document["kind"] == "deploys"
        assert list(document["data"]) == ["services"]
    changed = documents[1]["data"]["services"]
    assert {entry["service"] for entry in changed} == {"worker", "storydump"}, (
        "the watch's entries are keyed by service, not by a workspace"
    )
    assert any(c["change"] == "changed" for s in changed for c in s["changes"])


# --- doctor -------------------------------------------------------------------


def repo_migrations(tmp_path, *versions: int) -> Path:
    directory = tmp_path / "repo" / "scripts" / "migrations"
    directory.mkdir(parents=True)
    for version in versions:
        (directory / f"{version:03d}_something.sql").write_text("-- x")
    return directory


def checks_of(document) -> dict[str, dict]:
    checks = document["data"]["checks"]
    assert [c["check"] for c in checks] == [
        "token",
        "api",
        "storage",
        "config",
        "railway",
        "ledger",
    ]
    for check in checks:
        assert set(check) == {"check", "state", "value", "fix"}
        assert check["state"] in ("ok", "wrong", "missing", "skipped")
    return {c["check"]: c for c in checks}


def test_doctor_reports_every_check_ok_and_exits_0(tmp_path):
    rt = env_runtime(tmp_path, health_api())
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    write_config(tmp_path, Config(api_url="https://api.test", token_storage="keychain"))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    checks = checks_of(document)
    assert document["data"]["ok"] is True
    assert all(c["state"] == "ok" for c in checks.values()), checks
    assert checks["token"]["value"].startswith("chris-mbp")
    assert "operator" in checks["token"]["value"]
    assert "2.1.0" in checks["api"]["value"]
    assert "svc_ingress" in checks["api"]["value"] and "{" not in checks["api"]["value"]
    assert "4.30.3" in checks["railway"]["value"]
    assert "@" not in checks["railway"]["value"], (
        "no account line in a value an agent pastes"
    )
    assert "77" in checks["ledger"]["value"]


def test_doctor_human_output_is_one_line_per_check(tmp_path):
    rt = env_runtime(tmp_path, health_api())
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "doctor")
    assert result.exit_code == EXIT_OK, result.output
    for check in ("token", "api", "storage", "config", "railway", "ledger"):
        assert re.search(rf"^{check}\s+ok\s", result.stdout, re.M), (
            check,
            result.stdout,
        )


def test_doctor_without_a_token_says_missing_with_the_login_fix_exit_3(tmp_path):
    rt = env_runtime(tmp_path, health_api(), token=None)
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_NOT_AUTHORIZED, result.output
    checks = checks_of(one_envelope(result))
    assert checks["token"]["state"] == "missing"
    assert "storydump login" in checks["token"]["fix"]
    assert checks["ledger"]["state"] == "skipped", "the ledger needs the token"


def test_doctor_with_a_token_the_api_refuses_says_wrong(tmp_path):
    api = health_api(
        {
            ("GET", "/api/v1/me/principal"): (
                401,
                {"reason": "not_authorized", "detail": "x"},
            )
        }
    )
    rt = env_runtime(tmp_path, api)
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    checks = checks_of(one_envelope(result))
    assert checks["token"]["state"] == "wrong"
    assert checks["api"]["state"] == "ok", (
        "the API answered; the token is what is wrong"
    )


def test_doctor_blames_the_api_not_the_token_for_a_5xx(tmp_path):
    """`/health` answers but `/me/principal` 503s: the token could not be
    checked (skipped), the API is what is wrong, and the exit is 4 — not
    "the API refuses this token" with a mint-a-token fix and exit 3."""
    api = health_api(
        {("GET", "/api/v1/me/principal"): (503, {"detail": "busy — try again"})}
    )
    rt = env_runtime(tmp_path, api)
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    checks = checks_of(one_envelope(result))
    assert checks["token"]["state"] == "skipped", checks["token"]
    assert "mint" not in checks["token"]["fix"]
    assert checks["api"]["state"] == "wrong" and "503" in checks["api"]["value"]


def test_doctor_never_says_ok_over_an_unchecked_token(tmp_path):
    """`/health` answers, then the connection drops on `/me/principal`: the
    token is skipped, the API is WRONG (it stopped answering), `ok` is false
    and the exit is 4 — never "everything ok" with the token unchecked."""
    api = health_api({("GET", "/api/v1/me/principal"): httpx.ConnectError("dropped")})
    rt = env_runtime(tmp_path, api)
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    document = one_envelope(result)
    assert document["data"]["ok"] is False
    checks = checks_of(document)
    assert checks["token"]["state"] == "skipped"
    assert (
        checks["api"]["state"] == "wrong" and "/me/principal" in checks["api"]["value"]
    )


def test_doctor_reads_a_5xx_health_as_wrong_not_missing(tmp_path):
    api = health_api({("GET", "/health"): (503, {"detail": "no engine"})})
    rt = env_runtime(tmp_path, api)
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    checks = checks_of(one_envelope(result))
    assert checks["api"]["state"] == "wrong" and "503" in checks["api"]["value"]


def test_doctor_reads_a_4xx_health_as_wrong(tmp_path):
    api = health_api({("GET", "/health"): (404, {"detail": "not found"})})
    rt = env_runtime(tmp_path, api)
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    checks = checks_of(one_envelope(result))
    assert checks["api"]["state"] == "wrong" and "404" in checks["api"]["value"]


def test_doctor_with_an_unreachable_api_says_so_and_exits_4(tmp_path):
    api = health_api(
        {
            ("GET", "/health"): httpx.ConnectError("down"),
            ("GET", "/api/v1/me/principal"): httpx.ConnectError("down"),
        }
    )
    rt = env_runtime(tmp_path, api)
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_API_UNREACHABLE
    checks = checks_of(one_envelope(result))
    assert checks["api"]["state"] == "missing"
    assert checks["ledger"]["state"] == "skipped"


def test_doctor_with_a_logged_out_railway_says_missing(tmp_path):
    rail = ScriptedRailway(
        railway_answers(
            {("whoami",): (1, "", "Unauthorized. Please login with `railway login`\n")}
        )
    )
    rt = env_runtime(tmp_path, health_api(), rail)
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_RAILWAY_UNREACHABLE
    checks = checks_of(one_envelope(result))
    assert checks["railway"]["state"] == "missing", (
        "the plan's checklist: a missing login is missing"
    )
    assert "railway login" in checks["railway"]["fix"]


def test_doctor_reads_a_checkout_behind_the_deployment_as_wrong(tmp_path):
    rt = env_runtime(tmp_path, health_api())
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 77))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_API_UNREACHABLE
    checks = checks_of(one_envelope(result))
    assert checks["ledger"]["state"] == "wrong"
    assert "077" in checks["ledger"]["value"] and "lacks" in checks["ledger"]["value"]
    assert "git pull" in checks["ledger"]["fix"]


def test_doctor_reads_the_api_from_health_alone(tmp_path):
    """A deployment whose scheduling surface answers 503 is still a reachable API."""
    api = health_api({("GET", "/health/scheduling"): (503, {"detail": "no engine"})})
    rt = env_runtime(tmp_path, api)
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    checks = checks_of(one_envelope(run(rt, "--json", "doctor")))
    assert checks["api"]["state"] == "ok"


def test_doctor_without_railway_says_missing_and_exits_5(tmp_path):
    rail = ScriptedRailway(
        railway_answers({("--version",): FileNotFoundError("railway")})
    )
    rt = env_runtime(tmp_path, health_api(), rail)
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_RAILWAY_UNREACHABLE
    checks = checks_of(one_envelope(result))
    assert checks["railway"]["state"] == "missing"
    assert "install" in checks["railway"]["fix"].lower()


def test_doctor_names_the_migrations_the_ledger_lacks(tmp_path):
    rt = env_runtime(tmp_path, health_api())
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 79))
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_API_UNREACHABLE, (
        "a deployment behind the repository"
    )
    checks = checks_of(one_envelope(result))
    assert checks["ledger"]["state"] == "wrong"
    assert "078" in checks["ledger"]["value"]


def gate(directory: Path, *versions: int) -> None:
    """Rewrite these files as GATED ones: the runner's `manual` directive."""
    for version in versions:
        (directory / f"{version:03d}_something.sql").write_text(
            "-- the deploy owes this file and never runs it\n-- runner:manual\nSELECT 1;\n"
        )


def test_doctor_reports_a_gated_migration_as_owed_not_as_a_deployment_behind(
    tmp_path,
):
    """079 and 080 carry the runner's `manual` directive: every deploy owes
    them and only the owner applies them (`apply --manual`), so until that
    window runs they are in the checkout and not in the ledger BY DESIGN.
    "Deploy main" would be the wrong fix, and exit 4 a standing false alarm."""
    rt = env_runtime(tmp_path, health_api())
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 80))
    gate(rt.migrations_dir, 78, 79)
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_OK, result.output
    ledger = checks_of(one_envelope(result))["ledger"]
    assert ledger["state"] == "ok"
    assert "owed" in ledger["value"] and "078, 079" in ledger["value"]
    assert ledger["fix"] == ""


def test_doctor_still_names_an_ordinary_migration_beside_a_gated_one(tmp_path):
    rt = env_runtime(tmp_path, health_api())
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 80))
    gate(rt.migrations_dir, 79)
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_API_UNREACHABLE
    ledger = checks_of(one_envelope(result))["ledger"]
    assert ledger["state"] == "wrong"
    assert "1 migration(s)" in ledger["value"] and "078" in ledger["value"]
    assert "deploy main" in ledger["fix"]


def test_a_mention_of_the_directive_in_prose_does_not_gate_a_file(tmp_path):
    rt = env_runtime(tmp_path, health_api())
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 79))
    (rt.migrations_dir / "078_something.sql").write_text(
        "-- the runner:manual directive is what gates the NEXT file\nSELECT 1;\n"
    )
    ledger = checks_of(one_envelope(run(rt, "--json", "doctor")))["ledger"]
    assert ledger["state"] == "wrong" and "078" in ledger["value"]


def test_doctor_reads_the_directive_as_the_runner_does():
    """The CLI does not import the runner (it depends on psycopg2; this
    package does not), so it reads the directive itself — and this pins the
    two readings equal over the real corpus."""
    from scripts.migration_runner import MIGRATIONS_DIR, discover_migrations
    from storydump_cli.commands.env import _gated_in

    by_the_runner = {m.version for m in discover_migrations(MIGRATIONS_DIR) if m.manual}
    assert _gated_in(MIGRATIONS_DIR) == by_the_runner == {79, 80}


def test_doctor_outside_a_checkout_skips_the_ledger_comparison(tmp_path):
    rt = env_runtime(tmp_path, health_api())
    rt.migrations_dir = tmp_path / "nowhere"
    result = run(rt, "--json", "doctor")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    checks = checks_of(document)
    assert checks["ledger"]["state"] == "skipped"
    assert document["data"]["ok"] is True, "a skipped check is not a wrong one"
    assert "77" in checks["ledger"]["value"], "what the API reports is still shown"


def test_doctor_reports_the_storage_backend_in_use(tmp_path):
    rt = env_runtime(tmp_path, health_api())
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    checks = checks_of(one_envelope(run(rt, "--json", "doctor")))
    assert checks["storage"]["value"] == "memory", (
        "the backend's name, never its content"
    )


def test_doctor_prints_no_secret(tmp_path):
    rt = env_runtime(tmp_path, health_api())
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "doctor")
    assert "sdt_" not in result.stdout


def test_doctor_reads_a_bad_config_file_as_wrong(tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    rt = env_runtime(tmp_path, health_api())
    rt.migrations_dir = repo_migrations(tmp_path, *range(1, 78))
    result = run(rt, "--json", "doctor")
    checks = checks_of(one_envelope(result))
    assert checks["config"]["state"] == "wrong"
    assert result.exit_code != EXIT_OK


def test_the_health_renderer_reads_the_taps_real_keys(tmp_path):
    """`/health`'s tap block is `TapMetrics.snapshot()` — `taps_total`,
    `answer_failed`, and the per-outcome counts nested under `taps`
    (`src/api/routes/webhooks.py`). The renderer once read `executed` and
    `replayed` at the TOP level: `executed` is a real outcome but lives one
    level down, and `replayed` is not a tap outcome at all (`TAP_OUTCOMES`).
    Two of its three cells were always blank, and the fixture above spelled
    them the renderer's way so nothing caught it — the same defect the pool
    block had, fixed as `POOL_FACTS` in #1324."""
    from src.api.routes.webhooks import TapMetrics
    from storydump_cli.output import TAP_FACTS

    emitted = set(TapMetrics().snapshot())
    assert set(TAP_FACTS) <= emitted, (
        f"the renderer reads {set(TAP_FACTS) - emitted} — the API emits {sorted(emitted)}"
    )
    rt = env_runtime(tmp_path, health_api())
    result = run(rt, "health")
    # the scalars, and the per-outcome breakdown the old spelling was reaching for
    assert "taps_total 13" in result.output and "answer_failed 0" in result.output
    assert "executed 12" in result.output and "older_card 1" in result.output


def test_the_health_renderer_reads_the_pools_real_keys(tmp_path):
    """`/health`'s pool block is `PoolWatch.snapshot()` — `size`, `checked_out`,
    `checked_out_peak` (`src/services/target/unit_of_work.py`). The renderer
    once read `in_use` and `peak`, keys the API never emits, and the fixture
    above spelled them the renderer's way, so nothing caught the blank cells."""
    from src.services.target.unit_of_work import PoolWatch
    from storydump_cli.output import POOL_FACTS

    class _NoEngine:
        sync_engine = None

    emitted = set(PoolWatch(_NoEngine()).snapshot())
    assert set(POOL_FACTS) <= emitted, (
        f"the renderer reads {set(POOL_FACTS) - emitted} — the API emits {sorted(emitted)}"
    )
    rt = env_runtime(tmp_path, health_api())
    result = run(rt, "health")
    assert "checked_out 1" in result.output and "checked_out_peak 3" in result.output
