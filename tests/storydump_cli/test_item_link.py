"""An item's link from the terminal (#1413 phase 7): ``link``.

ONE call to the command port per run, under a FRESH key like ``schedule``:
a link cleared and later set to the same address again is a new act, and must
not replay the first answer. Exactly one of a link or ``--clear``; the link
itself is the port's to judge, so the CLI keeps no second copy of the rule.

A refused link is said in the verb's own words, the rule whole, since the
shared sentence ("the arguments were refused") names none of it. A
``not_found`` names the item in the words ``schedule`` uses.
"""

from __future__ import annotations

import pytest

from src.services.target.vocabulary import (
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    LINK_URL_MAX,
    MISSING_SENTENCES,
    exit_code_for,
)
from storydump_cli.commands import writes
from tests.storydump_cli.test_main import one_envelope, run
from tests.storydump_cli.test_writes import (
    UUID,
    WS,
    body_of,
    posts,
    route,
    write_api,
    write_runtime,
)

ITEM = "3c6e0b8a-9d7f-4a1e-b2c3-4d5e6f7a8b9c"
LINK = "https://example.com/spring-sale"
SET = {"outcome": "executed", "media_item_id": ITEM, "link_url": LINK}
CLEARED = {"outcome": "executed", "media_item_id": ITEM, "link_url": None}


def link(tmp_path, *args, answer=(200, SET)):
    api = write_api({route(WS, "set_item_link"): answer})
    result = run(write_runtime(tmp_path, api), "link", ITEM, *args, "--workspace", WS)
    return api, result


def test_link_sends_the_item_and_the_link_under_a_fresh_key(tmp_path):
    api, result = link(tmp_path, LINK)
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert request.url.path == f"/api/v1/workspaces/{WS}/commands/set_item_link"
    assert request.headers["Idempotency-Key"] == f"set_item_link:{WS}:{UUID}"
    assert body_of(request) == {"media_item_id": ITEM, "link_url": LINK}
    assert f"link updated — item {ITEM}: {LINK}" in result.output


def test_clear_sends_a_null_link_and_says_there_is_none(tmp_path):
    api, result = link(tmp_path, "--clear", answer=(200, CLEARED))
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert body_of(request) == {"media_item_id": ITEM, "link_url": None}
    assert f"link updated — item {ITEM}: no link" in result.output


def test_a_replayed_answer_claims_no_link_it_was_not_given(tmp_path):
    api, result = link(tmp_path, LINK, answer=(200, {"outcome": "replayed"}))
    assert result.exit_code == EXIT_OK, result.output
    assert f"— item {ITEM}" in result.output
    assert "no link" not in result.output
    assert LINK not in result.output.split(f"item {ITEM}")[1]


@pytest.mark.parametrize("args", [(), (LINK, "--clear")])
def test_one_of_a_link_or_clear_and_nothing_is_sent_otherwise(tmp_path, args):
    api, result = link(tmp_path, *args)
    assert result.exit_code == EXIT_USAGE
    assert not posts(api)


def test_a_refused_link_states_the_rule_whole(tmp_path):
    api, result = link(
        tmp_path,
        "http://example.com/spring-sale",
        "--json",
        answer=(400, {"reason": "invalid_args", "detail": "command refused: …"}),
    )
    assert result.exit_code == exit_code_for(400, "invalid_args")
    error = one_envelope(result)["error"]
    assert error["reason"] == "invalid_args"
    assert "https://" in error["detail"]
    assert f"{LINK_URL_MAX:,} characters" in error["detail"]
    assert "no user name or password" in error["detail"]
    assert "--clear" in error["fix"]


def test_an_item_that_is_not_there_is_named_as_schedule_names_it(tmp_path):
    api, result = link(
        tmp_path,
        LINK,
        "--json",
        answer=(404, {"reason": "not_found", "facts": {"missing": "item"}}),
    )
    assert result.exit_code == EXIT_NOT_FOUND
    error = one_envelope(result)["error"]
    assert error["detail"] == MISSING_SENTENCES["item"]
    assert error["fix"] == writes.MISSING_FIXES["item"]


def test_each_run_is_a_new_attempt(tmp_path):
    """The same link twice is two keys: set, cleared, then set again must not
    replay the first answer."""
    uuids = iter(["11111111-1111-4111-8111-111111111111", UUID])
    api = write_api({route(WS, "set_item_link"): (200, SET)})
    rt = write_runtime(tmp_path, api)
    rt.uuid_fn = lambda: next(uuids)
    for _ in range(2):
        run(rt, "link", ITEM, LINK, "--workspace", WS)
    first, second = (r.headers["Idempotency-Key"] for r in posts(api))
    assert first != second
