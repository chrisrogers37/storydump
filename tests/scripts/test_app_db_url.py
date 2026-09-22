"""Connection URLs built from `DB_*` fields: `make init-db`'s and the harness's.

`make init-db` connects twice — psql builds the by-hand base, then the migration
runner applies every file. The recipe pasted ``DB_USER`` and ``DB_PASSWORD``
into the runner's ``postgresql://`` URL, so a password carrying ``@``, ``/`` or
``%`` was misread by libpq after psql had connected with it (the tear-out's
owner queue; PR #1394's lenses). The test harness built its URLs the same way
in three places. All four now encode through ``src.config.db_url``, and every
assertion reads the URL back through the parser that will read it — libpq's
(``psycopg2.extensions.parse_dsn``) or SQLAlchemy's (``make_url``) — never
``urllib``, which is not what connects.

For init-db, three layers: the helper alone; the Makefile's text (each field
reaches the helper quoted exactly as psql gets it, no field pasted into a URL,
no export of the ``?=`` defaults); and the Makefile run by ``make`` itself,
with a probe target built from init-db's own psql arguments and runner URL —
copied from the Makefile — asserting the two steps connect with the same user,
host, port, database and password however a ``.env`` spells them. Local
development only: deployed services carry their URLs whole.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Synthetic values, never credentials.
HOSTILE_USER = "a/b@c:d"
HOSTILE_PASSWORD = "p@ss/w%41rd:?# x"
HOSTILE_NAME = "café db/x?y"
SOCKET_DIR = "/var/run/postgresql"
AT_VALUE = "ab@cd"
ESCAPED_QUOTE = 'ab\\"cd'  # a .env `\"`, which the shell reads as `"`
DOLLAR_ON_THE_COMMAND_LINE = "ab$$cd"  # make's `$$` is one `$`


def _helper():
    # Imported per test, not at module level: on a tree without the helper each
    # test fails on its own line instead of one collection error hiding the
    # Makefile pins below.
    from scripts import app_db_url as helper

    return helper


def _libpq(url: str) -> dict:
    from psycopg2.extensions import parse_dsn

    return parse_dsn(url)


# -- the one encoding rule and the harness's builders --------------------------


def test_userinfo_round_trips_a_hostile_user_and_password():
    from src.config.db_url import userinfo

    url = f"postgresql://{userinfo(HOSTILE_USER, HOSTILE_PASSWORD)}localhost:5432/s"
    got = _libpq(url)
    assert (got["user"], got["password"]) == (HOSTILE_USER, HOSTILE_PASSWORD)


def test_the_suites_test_database_url_keeps_a_hostile_password():
    from src.config.settings import Settings

    s = Settings(
        _env_file=None,
        DB_USER=HOSTILE_USER,
        DB_PASSWORD=HOSTILE_PASSWORD,
        TEST_DB_NAME="storydump_test",
    )
    got = _libpq(s.test_database_url)
    assert (got["user"], got["password"], got["dbname"]) == (
        HOSTILE_USER,
        HOSTILE_PASSWORD,
        "storydump_test",
    )


def test_the_harness_asyncpg_url_keeps_a_hostile_password(monkeypatch):
    from sqlalchemy.engine import make_url

    from src.config.settings import settings
    from src.services.target import unit_of_work

    monkeypatch.setattr(settings, "DB_USER", HOSTILE_USER)
    monkeypatch.setattr(settings, "DB_PASSWORD", HOSTILE_PASSWORD)
    url = make_url(unit_of_work.async_database_url("storydump_test"))
    assert (url.username, url.password, url.database) == (
        HOSTILE_USER,
        HOSTILE_PASSWORD,
        "storydump_test",
    )


def test_the_gates_dsn_keeps_a_hostile_password():
    from tests.scripts.conftest import _dsn

    got = _libpq(_dsn("storydump_test", user=HOSTILE_USER, password=HOSTILE_PASSWORD))
    assert (got["user"], got["password"]) == (HOSTILE_USER, HOSTILE_PASSWORD)


# -- the init-db helper ---------------------------------------------------------


def test_every_part_round_trips_through_libpq():
    url = _helper().app_db_url(
        user=HOSTILE_USER,
        password=HOSTILE_PASSWORD,
        host=SOCKET_DIR,
        port="6543",
        name=HOSTILE_NAME,
    )
    assert _libpq(url) == {
        "user": HOSTILE_USER,
        "password": HOSTILE_PASSWORD,
        "host": SOCKET_DIR,
        "port": "6543",
        "dbname": HOSTILE_NAME,
    }


def test_an_ipv6_host_round_trips():
    url = _helper().app_db_url(
        user="dev", password="", host="::1", port="5432", name="s"
    )
    assert _libpq(url) == {"user": "dev", "host": "::1", "port": "5432", "dbname": "s"}


def test_no_user_and_no_password_are_left_to_libpq():
    url = _helper().app_db_url(
        user="", password="", host="localhost", port="5432", name="s"
    )
    assert _libpq(url) == {"host": "localhost", "port": "5432", "dbname": "s"}


def test_a_password_without_a_user_keeps_the_password():
    url = _helper().app_db_url(
        user="", password=AT_VALUE, host="localhost", port="5432", name="s"
    )
    assert _libpq(url)["password"] == AT_VALUE


def test_run_directly_the_defaults_are_the_makefiles(capsys):
    """The helper's own defaults (for a direct run) mirror the Makefile's `?=`
    lines; under make the recipe passes every field explicitly."""
    text = (ROOT / "Makefile").read_text()

    def default(name: str) -> str:
        m = re.search(rf"^{name} \?=[ \t]*(.*)$", text, re.M)
        assert m is not None, f"the Makefile has no `{name} ?=` line"
        return m.group(1).strip()

    helper = _helper()
    assert default("DB_HOST") == helper.DEFAULT_HOST
    assert default("DB_PORT") == helper.DEFAULT_PORT
    assert default("DB_NAME") == helper.DEFAULT_NAME
    assert default("DB_USER") == "$(USER)", "libpq's default is the login name"
    assert default("DB_PASSWORD") == ""
    assert helper.main({}) == 0
    assert _libpq(capsys.readouterr().out.strip()) == {
        "host": helper.DEFAULT_HOST,
        "port": helper.DEFAULT_PORT,
        "dbname": helper.DEFAULT_NAME,
    }


# -- the Makefile's text -------------------------------------------------------


@pytest.fixture(scope="module")
def makefile() -> str:
    return (ROOT / "Makefile").read_text()


def _runner_line(makefile: str) -> str:
    lines = [
        ln for ln in makefile.splitlines() if "scripts.migration_runner apply" in ln
    ]
    assert len(lines) == 1, lines
    return lines[0]


def test_init_db_hands_the_helper_each_field_as_psql_gets_it(makefile):
    """`$$(` is make's escape for the shell's `$(` (with one `$` make reads an
    empty make variable and the runner gets no URL). The password reaches the
    helper double-quoted, as in psql's `PGPASSWORD="$(DB_PASSWORD)"`; the other
    four bare, as in `PG_OPTS` and `-d $(DB_NAME)`."""
    line = _runner_line(makefile)
    assert '@DATABASE_URL="$$(' in line and 'python -m scripts.app_db_url)"' in line, (
        line
    )
    assert 'DB_PASSWORD="$(DB_PASSWORD)"' in line
    bare = [
        f
        for f in ("DB_USER", "DB_HOST", "DB_PORT", "DB_NAME")
        if f"{f}=$({f}) " not in line
    ]
    assert bare == [], bare
    assert "PG_OPTS = -h $(DB_HOST) -p $(DB_PORT) -U $(DB_USER)" in makefile
    assert 'PGPASSWORD="$(DB_PASSWORD)" psql $(PG_OPTS) -d $(DB_NAME) -q' in makefile


def test_the_makefile_pastes_no_field_into_a_url(makefile):
    assert not re.search(r"postgresql://\$\(DB_", makefile), (
        "a URL assembled from raw DB_* fields is back"
    )


def test_the_makefile_exports_no_db_field(makefile):
    """Exporting the `?=` defaults would change every other target's
    environment when there is no `.env` — `make test` would connect as the
    login name, where `Settings` defaults to `storydump_user`."""
    exports = re.findall(r"^[ \t]*export[ \t]+(.+)$", makefile, re.M)
    assert not [e for e in exports if "DB_" in e], exports


# -- the Makefile, run by make ------------------------------------------------

MAKE = shutil.which("make")
needs_make = pytest.mark.skipif(MAKE is None, reason="no make on this host")

#: What the probe prints: psql's own arguments (`PG_OPTS` and `-d`, word-split
#: exactly as psql receives them) with its `PGPASSWORD`, beside libpq's parse
#: of the runner's URL.
_SHOW = """\
import json, os, sys
from psycopg2.extensions import parse_dsn
flags = {"-h": "host", "-p": "port", "-U": "user", "-d": "dbname"}
psql, args, i = {}, sys.argv[1:], 0
while i < len(args):
    if args[i] in flags and i + 1 < len(args):
        psql[flags[args[i]]] = args[i + 1]
        i += 2
    else:
        psql.setdefault("extra", []).append(args[i])
        i += 1
psql["password"] = os.environ["PGPASSWORD"]
print(json.dumps({"psql": psql, "runner": parse_dsn(os.environ["DATABASE_URL"])}))
"""


def _through_make(tmp_path, *, args=(), dotenv=None, target="probe"):
    """`make` run on the repository's Makefile from *tmp_path* (so its
    `include .env` reads *dotenv*), with a probe target built from init-db's
    own psql line — its `PGPASSWORD` and its arguments — and its runner URL,
    all copied from the Makefile."""
    text = (ROOT / "Makefile").read_text()
    url_expr = re.search(
        r'DATABASE_URL="(.*?)" python -m scripts\.migration_runner apply', text
    )
    psql = re.search(
        r'PGPASSWORD="(.*?)" psql (\$\(PG_OPTS\) -d \$\(DB_NAME\)) -q', text
    )
    assert url_expr is not None and psql is not None, "init-db changed shape"
    show = tmp_path / "show.py"
    show.write_text(_SHOW)
    fields = tmp_path / "fields.py"
    fields.write_text(
        "import json, os\n"
        "print(json.dumps(sorted(k for k in os.environ if k.startswith('DB_'))))\n"
    )
    probe = tmp_path / "probe.mk"
    probe.write_text(
        f'probe:\n\t@PGPASSWORD="{psql.group(1)}" '
        f'DATABASE_URL="{url_expr.group(1)}" python {show} {psql.group(2)}\n'
        f"fields:\n\t@python {fields}\n"
    )
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv)
    run_env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "PYTHONPATH": str(ROOT),
        "HOME": str(tmp_path),
        "USER": "probe_login",  # `DB_USER ?= $(USER)`
    }
    out = subprocess.run(
        [MAKE, "-s", "-f", str(ROOT / "Makefile"), "-f", str(probe), target, *args],
        cwd=tmp_path,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _agree(got: dict) -> None:
    """psql and the runner connect with the same five values (libpq leaves an
    empty password out of the URL)."""
    psql, runner = got["psql"], got["runner"]
    assert "extra" not in psql, psql
    for key in ("user", "host", "port", "dbname"):
        assert runner.get(key) == psql.get(key), (key, got)
    assert runner.get("password", "") == psql["password"], got


@needs_make
def test_make_hands_the_runner_a_hostile_command_line_intact(tmp_path):
    name = "café-db@x"  # psql gets the name bare, so no space
    got = _through_make(
        tmp_path,
        args=(
            f"DB_USER={HOSTILE_USER}",
            f"DB_PASSWORD={HOSTILE_PASSWORD}",
            f"DB_HOST={SOCKET_DIR}",
            "DB_PORT=6543",
            f"DB_NAME={name}",
        ),
    )
    _agree(got)
    assert got["runner"] == {
        "user": HOSTILE_USER,
        "password": HOSTILE_PASSWORD,
        "host": SOCKET_DIR,
        "port": "6543",
        "dbname": name,
    }


#: How a `.env` may spell the fields, and the password the shell makes of each
#: in psql's step — and so, now, in the runner's. The shell's answer is not
#: always the value meant (single quotes are literal inside psql's double
#: quotes; a comment's leading space stays): that is every psql recipe's limit,
#: recorded here, and the property pinned is that the two steps agree. Built
#: from constants, never a literal.
DOTENV_CASES = {
    "bare password": (f"DB_USER=dev\nDB_PASSWORD={AT_VALUE}\n", AT_VALUE),
    "double-quoted password": (f'DB_USER=dev\nDB_PASSWORD="{AT_VALUE}"\n', AT_VALUE),
    "single-quoted password": (
        f"DB_USER=dev\nDB_PASSWORD='{AT_VALUE}'\n",
        f"'{AT_VALUE}'",
    ),
    "escaped quote in the password": (
        f'DB_USER=dev\nDB_PASSWORD="{ESCAPED_QUOTE}"\n',
        'ab"cd',
    ),
    "password with a trailing comment": (
        f'DB_USER=dev\nDB_PASSWORD="{AT_VALUE}" # a note\n',
        f"{AT_VALUE} ",
    ),
    "single-quoted host": ("DB_USER=dev\nDB_HOST='localhost'\n", ""),
    "double-quoted user": ('DB_USER="dev"\n', ""),
    "database name with a trailing comment": (
        "DB_USER=dev\nDB_NAME=storydump # local\n",
        "",
    ),
    "port with a trailing comment": ("DB_USER=dev\nDB_PORT=5432 # local\n", ""),
}


@needs_make
@pytest.mark.parametrize("case", sorted(DOTENV_CASES))
def test_the_runner_connects_with_what_psql_does(tmp_path, case):
    dotenv, password = DOTENV_CASES[case]
    got = _through_make(tmp_path, dotenv=dotenv)
    _agree(got)
    assert got["psql"]["password"] == password


@needs_make
def test_a_dollar_that_starts_a_name_is_expanded_in_both_steps_alike(tmp_path):
    """make turns `$$` into `$`, then the shell line expands `$cd` — in psql's
    step and the runner's alike. What is pinned is that neither step disagrees."""
    got = _through_make(
        tmp_path, args=("DB_USER=dev", f"DB_PASSWORD={DOLLAR_ON_THE_COMMAND_LINE}")
    )
    _agree(got)
    assert got["psql"]["password"] == "ab"


@needs_make
def test_make_with_nothing_set_hands_the_runner_the_defaults(tmp_path):
    got = _through_make(tmp_path)
    _agree(got)
    assert got["runner"] == {
        "user": "probe_login",
        "host": "localhost",
        "port": "5432",
        "dbname": "storydump",
    }


@needs_make
def test_without_a_dotenv_no_db_field_reaches_another_target(tmp_path):
    assert _through_make(tmp_path, target="fields") == []
