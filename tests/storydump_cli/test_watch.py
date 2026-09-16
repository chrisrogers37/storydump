"""``--watch``: a scripted sequence of API answers, a sleeper that never
sleeps, and a fixed clock.

Pinned: only added, changed and removed rows print (keyed by the view's
id column); ``--json`` emits one envelope per read whose ``data`` is
``{"workspaces": [{"workspace_id", "changes"}]}``; the verb's terminal
condition ends with 0 (``floating`` empty twice running, ``burst`` with no
row mid-flight); its failure condition ends with 6 and one error envelope;
Ctrl-C ends with 0; an API error mid-watch is the same answer it is
anywhere else; a token inside a row never reaches the terminal.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.services.target.vocabulary import (
    EXIT_API_UNREACHABLE,
    EXIT_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_WATCH_FAILED,
    check_envelope,
)
from storydump_cli.watch import WATCHED, diff
from tests.storydump_cli.test_main import SECRET, Api, run
from tests.storydump_cli.test_reads import (
    INTENT,
    OTHER_INTENT,
    WS_A,
    WS_B,
    burst_row,
    floating_row,
    jobs_row,
    outbox_row,
    path,
    read_runtime,
    reads_api,
    story_row,
    view,
)


class Script(Api):
    """An ``Api`` whose route may answer a SEQUENCE: each call pops the next
    answer and the last one sticks, so a watch that reads past the script
    sees a steady state rather than a 404."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        answer = self.routes.get((request.method, request.url.path))
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if answer is None:
            return httpx.Response(404, json={"detail": "not found"})
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)


class Sleeper:
    """Records every sleep; raises Ctrl-C after *interrupt_after* sleeps, and
    fails the test outright when a watch that should have ended keeps going."""

    def __init__(self, *, interrupt_after=None, limit=12) -> None:
        self.calls: list[float] = []
        self.interrupt_after = interrupt_after
        self.limit = limit

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.interrupt_after is not None and len(self.calls) >= self.interrupt_after:
            raise KeyboardInterrupt
        if len(self.calls) >= self.limit:
            raise AssertionError("the watch did not end")


def script(kind: str, reads: list, *, ws: str = WS_A, key=None) -> Script:
    """A scripted API for one workspace: *reads* is the sequence the view
    answers, one per read — a list of rows, or a ready ``(status, body)``."""
    api = Script(reads_api().routes)
    api.routes[("GET", path(kind, ws, key))] = [
        item if isinstance(item, tuple) else view(kind, ws, item) for item in reads
    ]
    return api


def watch_runtime(tmp_path, api, sleeper: Sleeper):
    rt = read_runtime(tmp_path, api)
    rt.sleep_fn = sleeper
    return rt


def envelopes(result) -> list[dict]:
    documents = [json.loads(line) for line in result.stdout.splitlines()]
    for document in documents:
        check_envelope(document)
    return documents


def changes_of(document, ws=WS_A) -> list[tuple[str, dict]]:
    data = document["data"]
    assert list(data.keys()) == ["workspaces"]
    for entry in data["workspaces"]:
        assert list(entry.keys()) == ["workspace_id", "changes"], entry
        if entry["workspace_id"] == ws:
            return [(c["change"], c["row"]) for c in entry["changes"]]
    raise AssertionError(f"{ws} not in {data}")


# --- only what changed prints ------------------------------------------------------


def test_floating_watch_prints_added_changed_and_removed_rows_only(tmp_path):
    first, second = floating_row(WS_A), floating_row(WS_A, OTHER_INTENT)
    api = script(
        "floating",
        [
            [first, second],
            [floating_row(WS_A, job_state="leased"), second],
            [],
            [],
        ],
    )
    sleeper = Sleeper()
    result = run(
        watch_runtime(tmp_path, api, sleeper),
        "floating",
        "--watch",
        "--every",
        "5",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_OK, result.output
    lines = result.stdout.splitlines()
    assert [line.split()[1] for line in lines] == [
        "added",
        "added",
        "changed",
        "removed",
        "removed",
    ], result.stdout
    assert all(line.startswith("15:00:00") for line in lines), "every line is stamped"
    assert all(line.split()[2] == WS_A for line in lines)
    assert INTENT in lines[0] and OTHER_INTENT in lines[1]
    assert INTENT in lines[2] and "leased" in lines[2]
    assert OTHER_INTENT not in lines[2], "an unchanged row does not print again"
    assert sleeper.calls == [5, 5, 5], "the interval, between reads, until empty twice"
    assert len([p for p in api.paths() if "/ops/" in p]) == 4


def test_floating_watch_json_is_one_envelope_per_read(tmp_path):
    first, second = floating_row(WS_A), floating_row(WS_A, OTHER_INTENT)
    leased = floating_row(WS_A, job_state="leased")
    api = script("floating", [[first, second], [leased, second], [], []])
    result = run(
        watch_runtime(tmp_path, api, Sleeper()),
        "--json",
        "floating",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_OK, result.output
    documents = envelopes(result)
    assert len(documents) == 4, "one envelope per read, an empty one included"
    assert {d["kind"] for d in documents} == {"floating"}
    assert changes_of(documents[0]) == [("added", first), ("added", second)]
    assert changes_of(documents[1]) == [("changed", leased)]
    assert changes_of(documents[2]) == [("removed", leased), ("removed", second)]
    assert changes_of(documents[3]) == []


def test_floating_watch_default_interval_is_thirty_seconds(tmp_path):
    api = script("floating", [[floating_row(WS_A)], [], []])
    sleeper = Sleeper()
    result = run(
        watch_runtime(tmp_path, api, sleeper),
        "floating",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_OK, result.output
    assert sleeper.calls == [30, 30]


def test_floating_watch_needs_two_empty_reads_in_a_row(tmp_path):
    api = script("floating", [[], [floating_row(WS_A)], [], []])
    sleeper = Sleeper()
    result = run(
        watch_runtime(tmp_path, api, sleeper),
        "floating",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_OK, result.output
    assert len(sleeper.calls) == 3
    assert [line.split()[1] for line in result.stdout.splitlines()] == [
        "added",
        "removed",
    ]


# --- the failure condition ------------------------------------------------------------


def test_floating_watch_exits_6_when_a_job_has_failed(tmp_path):
    api = script(
        "floating", [[floating_row(WS_A)], [floating_row(WS_A, job_state="failed")]]
    )
    result = run(
        watch_runtime(tmp_path, api, Sleeper()),
        "floating",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_WATCH_FAILED, result.output
    lines = result.stdout.splitlines()
    assert [line.split()[1] for line in lines] == ["added", "changed"]
    assert "failed" in lines[1]
    assert result.stderr.startswith("error: ")
    assert "fix: " in result.stderr


def test_floating_watch_json_failure_is_the_read_then_one_error_envelope(tmp_path):
    failed = floating_row(WS_A, job_state="failed")
    api = script("floating", [[floating_row(WS_A)], [failed]])
    result = run(
        watch_runtime(tmp_path, api, Sleeper()),
        "--json",
        "floating",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_WATCH_FAILED, result.output
    documents = envelopes(result)
    assert len(documents) == 3
    assert changes_of(documents[1]) == [("changed", failed)]
    error = documents[2]["error"]
    assert documents[2]["kind"] == "floating"
    assert error["code"] == EXIT_WATCH_FAILED
    assert error["reason"] == "watch_failed"
    assert result.stderr == ""


def test_a_failure_already_present_on_the_first_read_is_printed_not_fatal(tmp_path):
    """The first read is the baseline: a float whose retry died before the
    watch began is shown, and the watch keeps waiting for what ARRIVES — a
    window that never slides can still be waited on."""
    failed = floating_row(WS_A, job_state="failed")
    api = script("floating", [[failed], [failed], [failed]])
    sleeper = Sleeper(interrupt_after=2)
    result = run(
        watch_runtime(tmp_path, api, sleeper),
        "floating",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_OK, result.output
    assert len(sleeper.calls) == 2
    assert [line.split()[1] for line in result.stdout.splitlines()] == ["added"]


def test_a_failed_group_that_grows_is_a_change_that_ends_the_watch(tmp_path):
    api = script(
        "jobs",
        [
            [jobs_row(WS_A, state="failed", count=1)],
            [jobs_row(WS_A, state="failed", count=2)],
        ],
    )
    result = run(
        watch_runtime(tmp_path, api, Sleeper()), "jobs", "--watch", "--workspace", WS_A
    )
    assert result.exit_code == EXIT_WATCH_FAILED, result.output
    assert [line.split()[1] for line in result.stdout.splitlines()] == [
        "added",
        "changed",
    ]


def test_a_failed_group_that_shrinks_is_a_change_that_does_not_end_the_watch(tmp_path):
    """The runbook's rule: exit 6 when a failed group APPEARS or GROWS. Two
    failed jobs becoming one is a repair in progress — still printed, never
    the failure."""
    api = script(
        "jobs",
        [
            [jobs_row(WS_A, state="failed", count=2)],
            [jobs_row(WS_A, state="failed", count=1)],
            [jobs_row(WS_A, state="failed", count=1)],
        ],
    )
    sleeper = Sleeper(interrupt_after=3)
    result = run(
        watch_runtime(tmp_path, api, sleeper), "jobs", "--watch", "--workspace", WS_A
    )
    assert result.exit_code == EXIT_OK, result.output
    assert [line.split()[1] for line in result.stdout.splitlines()] == [
        "added",
        "changed",
    ]
    api = script(
        "outbox",
        [
            [outbox_row(WS_A, state="failed", count=3)],
            [outbox_row(WS_A, state="failed", count=2)],
        ],
    )
    result = run(
        watch_runtime(tmp_path, api, Sleeper(interrupt_after=2)),
        "outbox",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_OK, result.output


def test_a_floating_story_whose_job_recovers_is_not_the_failure(tmp_path):
    """A floating row changing from a failed job to a ready one is the retry
    landing; only a row whose job BECOMES failed ends the watch."""
    api = script(
        "floating",
        [
            [floating_row(WS_A, job_state="failed")],
            [floating_row(WS_A, job_state="ready")],
            [floating_row(WS_A, job_state="ready")],
        ],
    )
    result = run(
        watch_runtime(tmp_path, api, Sleeper(interrupt_after=3)),
        "floating",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_OK, result.output


def test_burst_keys_tell_two_permits_and_two_siblings_apart():
    key = WATCHED["burst"].key
    first = burst_row(WS_A, "permit", generation=1, state="failed", url_variant=0)
    second = burst_row(WS_A, "permit", generation=2, state="succeeded", url_variant=1)
    assert first["at"] == second["at"] and first["intent_id"] == second["intent_id"]
    assert key(first) != key(second), "two permits of one story in one transaction"
    one = burst_row(WS_A, "sibling", posted_id="p", waiting_id="w1", wait_class="fetch")
    two = burst_row(WS_A, "sibling", posted_id="p", waiting_id="w2", wait_class="fetch")
    assert key(one) != key(two), "one story posting past two waiters"


def test_burst_watch_ends_when_no_row_is_mid_flight(tmp_path):
    permitted = burst_row(
        WS_A, "permit", generation=1, state="permitted", url_variant="fresh"
    )
    published = {**permitted, "state": "published"}
    publishing = burst_row(
        WS_A, "outcome", at=None, intent_id=None, state="publishing", count=1
    )
    posted = burst_row(
        WS_A, "outcome", at=None, intent_id=None, state="posted", count=1
    )
    api = script(
        "burst", [[permitted, publishing], [published, publishing], [published, posted]]
    )
    sleeper = Sleeper()
    result = run(
        watch_runtime(tmp_path, api, sleeper), "burst", "--watch", "--workspace", WS_A
    )
    assert result.exit_code == EXIT_OK, result.output
    assert len(sleeper.calls) == 2, (
        "a permit still permitted, then a story still publishing"
    )
    kinds = [line.split()[1] for line in result.stdout.splitlines()]
    assert kinds == ["added", "added", "changed", "added", "removed"], (
        "the permit changed; in the census, posted appeared and publishing went"
    )


def test_burst_watch_keeps_watching_a_lost_answer(tmp_path):
    """`publishing_ambiguous` is a story whose publish answer was lost — still
    on its way as far as the ledger knows. Ending the watch on it would exit 0
    on exactly the case the post-deploy read exists to catch."""
    ambiguous = burst_row(
        WS_A, "outcome", at=None, intent_id=None, state="publishing_ambiguous", count=1
    )
    posted = burst_row(
        WS_A, "outcome", at=None, intent_id=None, state="posted", count=1
    )
    api = script("burst", [[ambiguous], [ambiguous], [posted]])
    sleeper = Sleeper()
    result = run(
        watch_runtime(tmp_path, api, sleeper), "burst", "--watch", "--workspace", WS_A
    )
    assert result.exit_code == EXIT_OK, result.output
    assert len(sleeper.calls) == 2, "two reads with the lost answer unresolved"


def test_burst_watch_with_nothing_in_flight_ends_after_one_read(tmp_path):
    api = script(
        "burst",
        [
            [
                burst_row(
                    WS_A, "outcome", at=None, intent_id=None, state="posted", count=13
                )
            ]
        ],
    )
    sleeper = Sleeper()
    result = run(
        watch_runtime(tmp_path, api, sleeper), "burst", "--watch", "--workspace", WS_A
    )
    assert result.exit_code == EXIT_OK, result.output
    assert sleeper.calls == []


def test_burst_watch_exits_6_on_a_review_row(tmp_path):
    permitted = burst_row(
        WS_A, "permit", generation=1, state="permitted", url_variant="fresh"
    )
    review = burst_row(WS_A, "review", from_state="publishing", last_error="ambiguous")
    api = script("burst", [[permitted], [permitted, review]])
    result = run(
        watch_runtime(tmp_path, api, Sleeper()),
        "--json",
        "burst",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_WATCH_FAILED, result.output
    documents = envelopes(result)
    assert changes_of(documents[1]) == [("added", review)]
    assert documents[-1]["error"]["reason"] == "watch_failed"
    assert "review" in documents[-1]["error"]["detail"]


def test_jobs_watch_exits_6_on_a_failed_group(tmp_path):
    ready = jobs_row(WS_A)
    failed = jobs_row(WS_A, state="failed", count=1)
    api = script("jobs", [[ready], [ready, failed]])
    result = run(
        watch_runtime(tmp_path, api, Sleeper()), "jobs", "--watch", "--workspace", WS_A
    )
    assert result.exit_code == EXIT_WATCH_FAILED, result.output
    assert [line.split()[1] for line in result.stdout.splitlines()] == [
        "added",
        "added",
    ]


def test_outbox_watch_exits_6_on_a_failed_group(tmp_path):
    pending = outbox_row(WS_A)
    failed = outbox_row(WS_A, state="failed")
    api = script("outbox", [[pending], [pending, failed]])
    result = run(
        watch_runtime(tmp_path, api, Sleeper()),
        "outbox",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_WATCH_FAILED, result.output


# --- no terminal condition: until Ctrl-C ---------------------------------------------


def test_ctrl_c_ends_with_exit_0(tmp_path):
    api = script("jobs", [[jobs_row(WS_A)]])
    sleeper = Sleeper(interrupt_after=3)
    result = run(
        watch_runtime(tmp_path, api, sleeper), "jobs", "--watch", "--workspace", WS_A
    )
    assert result.exit_code == EXIT_OK, result.output
    assert len(sleeper.calls) == 3
    assert [line.split()[1] for line in result.stdout.splitlines()] == ["added"]
    assert result.stderr == ""


def test_ctrl_c_in_json_mode_leaves_only_the_read_envelopes(tmp_path):
    api = script("jobs", [[jobs_row(WS_A)]])
    result = run(
        watch_runtime(tmp_path, api, Sleeper(interrupt_after=2)),
        "--json",
        "jobs",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_OK, result.output
    documents = envelopes(result)
    assert len(documents) == 2
    assert all(d["error"] is None for d in documents)


def test_story_watch_keys_on_the_intent_and_prints_a_state_change(tmp_path):
    api = script(
        "story",
        [
            [story_row(WS_A)],
            [story_row(WS_A, state="publishing")],
            [story_row(WS_A, state="publishing")],
        ],
        key=INTENT,
    )
    result = run(
        watch_runtime(tmp_path, api, Sleeper(interrupt_after=3)),
        "story",
        INTENT,
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_OK, result.output
    lines = result.stdout.splitlines()
    assert [line.split()[1] for line in lines] == ["added", "changed"]
    assert INTENT in lines[1] and "publishing" in lines[1]


def test_cards_and_account_watch_until_ctrl_c(tmp_path):
    from tests.storydump_cli.test_reads import account_row, cards_row

    api = script("cards", [[cards_row(WS_A)]], key=INTENT)
    api.routes[("GET", path("account", WS_A, "storydump.studio"))] = [
        view("account", WS_A, [account_row(WS_A)])
    ]
    rt = watch_runtime(tmp_path, api, Sleeper(interrupt_after=2))
    assert run(rt, "cards", INTENT, "--watch", "--workspace", WS_A).exit_code == EXIT_OK
    rt.sleep_fn = Sleeper(interrupt_after=2)
    result = run(rt, "account", "storydump.studio", "--watch", "--workspace", WS_A)
    assert result.exit_code == EXIT_OK, result.output
    assert [line.split()[1] for line in result.stdout.splitlines()] == ["added"]


# --- several workspaces, errors, redaction ---------------------------------------------


def test_a_watch_over_two_workspaces_keys_rows_per_workspace(tmp_path):
    api = Script(reads_api().routes)
    api.routes[("GET", path("floating", WS_A))] = [
        view("floating", WS_A, [floating_row(WS_A)]),
        view("floating", WS_A, [floating_row(WS_A)]),
        view("floating", WS_A, []),
    ]
    api.routes[("GET", path("floating", WS_B))] = [
        view("floating", WS_B, []),
        view("floating", WS_B, [floating_row(WS_B)]),
        view("floating", WS_B, []),
    ]
    result = run(
        watch_runtime(tmp_path, api, Sleeper()), "--json", "floating", "--watch"
    )
    assert result.exit_code == EXIT_OK, result.output
    documents = envelopes(result)
    assert len(documents) == 4
    assert changes_of(documents[0], WS_A) == [("added", floating_row(WS_A))]
    assert changes_of(documents[0], WS_B) == []
    assert changes_of(documents[1], WS_A) == []
    assert changes_of(documents[1], WS_B) == [("added", floating_row(WS_B))]
    assert changes_of(documents[2], WS_A) == [("removed", floating_row(WS_A))]
    assert changes_of(documents[2], WS_B) == [("removed", floating_row(WS_B))]


def test_an_api_error_mid_watch_is_the_usual_answer(tmp_path):
    api = script("floating", [[floating_row(WS_A)], (404, {"detail": "not found"})])
    result = run(
        watch_runtime(tmp_path, api, Sleeper()),
        "--json",
        "floating",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_NOT_AUTHORIZED, result.output
    documents = envelopes(result)
    assert len(documents) == 2
    assert documents[1]["error"]["reason"] == "not_a_member"


def test_a_transient_failure_mid_watch_is_retried_before_it_ends_the_watch(tmp_path):
    """A 503 (the pool saturated, a deploy in flight) or a dropped connection
    is not the answer a watch was waiting for: it re-reads up to three times
    in a row before giving the usual exit 4. A definitive answer (a 404, a
    403) still ends the watch at once — pinned above."""
    row = floating_row(WS_A)
    api = Script(reads_api().routes)
    api.routes[("GET", path("floating", WS_A))] = [
        view("floating", WS_A, [row]),
        (503, {"detail": "busy — try again", "reason": "pool_saturated"}),
        httpx.ConnectError("dropped"),
        view("floating", WS_A, [floating_row(WS_A, job_state="leased")]),
        view("floating", WS_A, []),
        view("floating", WS_A, []),
    ]
    sleeper = Sleeper()
    result = run(
        watch_runtime(tmp_path, api, sleeper),
        "--json",
        "floating",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_OK, result.output
    documents = envelopes(result)
    assert [d["error"] for d in documents] == [None] * len(documents), (
        "a retried failure prints nothing"
    )
    assert changes_of(documents[1], WS_A) == [
        ("changed", floating_row(WS_A, job_state="leased"))
    ]
    assert len(sleeper.calls) == 5, "it slept through the two failed reads"


def test_a_failure_that_persists_ends_the_watch_with_the_usual_answer(tmp_path):
    api = script(
        "floating",
        [
            [floating_row(WS_A)],
            (503, {"detail": "busy", "reason": "pool_saturated"}),
            (503, {"detail": "busy", "reason": "pool_saturated"}),
            (503, {"detail": "busy", "reason": "pool_saturated"}),
            (503, {"detail": "busy", "reason": "pool_saturated"}),
        ],
    )
    result = run(
        watch_runtime(tmp_path, api, Sleeper()),
        "--json",
        "floating",
        "--watch",
        "--workspace",
        WS_A,
    )
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    documents = envelopes(result)
    assert documents[-1]["error"]["reason"] == "api_unreachable"
    assert "503" in documents[-1]["error"]["detail"]


def test_watch_lines_are_redacted_in_both_modes(tmp_path):
    leaked = floating_row(WS_A, last_wait_class=f"x {SECRET}")
    rt = watch_runtime(tmp_path, script("floating", [[leaked], [], []]), Sleeper())
    result = run(rt, "floating", "--watch", "--workspace", WS_A)
    assert result.exit_code == EXIT_OK, result.output
    assert SECRET not in result.output and "sdt_…" in result.stdout
    rt = watch_runtime(tmp_path, script("floating", [[leaked], [], []]), Sleeper())
    result = run(rt, "--json", "floating", "--watch", "--workspace", WS_A)
    assert SECRET not in result.output
    assert changes_of(envelopes(result)[0])[0][1]["last_wait_class"] == "x sdt_…"


# --- the diff and the keys, on their own ------------------------------------------------


def test_diff_reports_added_changed_and_removed_per_workspace():
    key = WATCHED["floating"].key
    a1, a2 = floating_row(WS_A), floating_row(WS_A, OTHER_INTENT)
    previous = [
        {"workspace_id": WS_A, "rows": [a1, a2]},
        {"workspace_id": WS_B, "rows": []},
    ]
    current = [
        {"workspace_id": WS_A, "rows": [floating_row(WS_A, job_state="leased")]},
        {"workspace_id": WS_B, "rows": [floating_row(WS_B)]},
    ]
    assert diff(None, previous, key) == [
        {
            "workspace_id": WS_A,
            "changes": [{"change": "added", "row": a1}, {"change": "added", "row": a2}],
        },
        {"workspace_id": WS_B, "changes": []},
    ]
    assert diff(previous, current, key) == [
        {
            "workspace_id": WS_A,
            "changes": [
                {"change": "changed", "row": floating_row(WS_A, job_state="leased")},
                {"change": "removed", "row": a2},
            ],
        },
        {
            "workspace_id": WS_B,
            "changes": [{"change": "added", "row": floating_row(WS_B)}],
        },
    ]


def test_diff_reports_a_workspace_that_vanished_as_removed_rows():
    key = WATCHED["floating"].key
    previous = [{"workspace_id": WS_B, "rows": [floating_row(WS_B)]}]
    assert diff(previous, [], key) == [
        {
            "workspace_id": WS_B,
            "changes": [{"change": "removed", "row": floating_row(WS_B)}],
        }
    ]


@pytest.mark.parametrize(
    ("kind", "row", "expected"),
    [
        ("cards", {"id": "c1"}, "c1"),
        ("floating", {"id": "f1"}, "f1"),
        ("account", {"id": "a1"}, "a1"),
        (
            "jobs",
            {"kind": "publish", "lane": "publish", "state": "ready"},
            ("publish", "publish", "ready"),
        ),
        (
            "outbox",
            {"binding_id": "b", "kind": "approval", "state": "pending"},
            ("b", "approval", "pending"),
        ),
        (
            "burst",
            {"section": "tap", "at": "t", "intent_id": "i"},
            ("tap", "t", "i", None, None, None),
        ),
        ("story", {"intent": {"id": "s1"}}, "s1"),
    ],
)
def test_the_key_of_each_view(kind, row, expected):
    assert WATCHED[kind].key(row) == expected


def test_two_outcome_rows_never_share_a_key():
    key = WATCHED["burst"].key
    posted = {
        "section": "outcome",
        "at": None,
        "intent_id": None,
        "state": "posted",
        "count": 13,
    }
    skipped = {**posted, "state": "skipped", "count": 2}
    assert key(posted) != key(skipped)


def test_posture_is_not_watched():
    assert "posture" not in WATCHED
