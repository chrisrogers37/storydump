"""`make init-db`'s runner URL, built from the Makefile's DB_* fields.

The Makefile pasted the fields into a URL raw —
``postgresql://$(DB_USER):$(DB_PASSWORD)@$(DB_HOST):...`` — so a password
carrying ``@``, ``:``, ``/`` or ``%`` parsed into the host or the port, and the
runner connected somewhere else or not at all (the tear-out's owner queue,
2026-09-18). ``scripts.app_db_url`` builds it with every part percent-encoded;
these pin that the password survives the round trip and that the Makefile
takes its URL from the helper rather than pasting the fields again. Local
development only: deployed services carry ``DATABASE_URL`` whole.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]

HOSTILE = "p@ss:w/rd%20?#'\" x"


def _helper():
    # Imported per test, not at module level: on a tree without the helper each
    # test fails on its own line instead of one collection error hiding the
    # Makefile pins below.
    from scripts import app_db_url as helper

    return helper


def _parts(url: str):
    u = urlsplit(url)
    return (
        unquote(u.username or ""),
        None if u.password is None else unquote(u.password),
        u.hostname,
        u.port,
        u.path,
    )


def test_a_password_with_reserved_characters_stays_the_password():
    url = _helper().app_db_url(
        user="dev", password=HOSTILE, host="localhost", port="5432", name="storydump"
    )
    assert _parts(url) == ("dev", HOSTILE, "localhost", 5432, "/storydump")


def test_a_user_with_reserved_characters_stays_the_user():
    url = _helper().app_db_url(
        user="a@b:c", password="pw", host="db.local", port="6543", name="x"
    )
    assert _parts(url) == ("a@b:c", "pw", "db.local", 6543, "/x")


def test_no_password_leaves_none_in_the_url():
    url = _helper().app_db_url(
        user="dev", password="", host="localhost", port="5432", name="s"
    )
    assert _parts(url) == ("dev", None, "localhost", 5432, "/s")


def test_main_reads_the_fields_with_the_makefiles_defaults(capsys):
    assert _helper().main({"DB_USER": "dev", "DB_PASSWORD": HOSTILE}) == 0
    url = capsys.readouterr().out.strip()
    assert _parts(url) == ("dev", HOSTILE, "localhost", 5432, "/storydump")


@pytest.fixture(scope="module")
def makefile() -> str:
    return (ROOT / "Makefile").read_text()


def test_init_db_takes_its_url_from_the_helper(makefile):
    runner_lines = [
        line
        for line in makefile.splitlines()
        if "scripts.migration_runner apply" in line
    ]
    assert runner_lines, "init-db no longer runs the migration runner"
    assert all("python -m scripts.app_db_url" in line for line in runner_lines), (
        runner_lines
    )


def test_the_makefile_pastes_no_field_into_a_url(makefile):
    assert not re.search(r"postgresql://\$\(DB_", makefile), (
        "a URL assembled from raw DB_* fields is back"
    )


def test_the_fields_reach_the_helper_without_a_dotenv(makefile):
    """`export` inside the `.env` block exports nothing when there is no
    `.env`, so the Makefile's own `?=` defaults must be exported by name."""
    exported = re.search(r"^export (.+)$", makefile, re.M)
    assert exported is not None, "no named export line"
    names = set(exported.group(1).split())
    assert {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"} <= names
