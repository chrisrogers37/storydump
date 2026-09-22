"""`make init-db`'s runner URL, built by `scripts.app_db_url`.

`make init-db` connects twice — psql builds the by-hand base, then the migration
runner applies every file. The recipe pasted ``DB_USER`` and ``DB_PASSWORD``
into the runner's ``postgresql://`` URL, so a password carrying ``@``, ``/`` or
``%`` was misread by libpq after psql had connected with it (the tear-out's
owner queue; PR #1394's lenses). Every assertion reads the URL back through
libpq's own parser (``psycopg2.extensions.parse_dsn``), the one the runner's
driver uses — not ``urllib``, which is not what connects.

Three layers: the helper alone; the Makefile's text (the recipe hands each field
to the helper through psql's own quoting, no field pasted into a URL, no export
of the ``?=`` defaults); and the Makefile run by ``make`` itself, with a probe
target built from init-db's two expressions — psql's ``PGPASSWORD`` and the
runner's ``DATABASE_URL``, copied from the Makefile — so what is asserted is
that the two steps connect with the same password, however a ``.env`` spells
it. Local development only: deployed services carry ``DATABASE_URL`` whole.
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


# -- the helper ----------------------------------------------------------------


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

FIELDS = ("DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT", "DB_NAME")


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
    empty make variable and the runner gets no URL), and each field reaches the
    helper through the quoting psql's `PGPASSWORD="$(DB_PASSWORD)"` gets."""
    line = _runner_line(makefile)
    assert '@DATABASE_URL="$$(' in line and 'python -m scripts.app_db_url)"' in line, (
        line
    )
    missing = [f for f in FIELDS if f'{f}="$({f})"' not in line]
    assert missing == [], missing
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


def _through_make(tmp_path, *, args=(), dotenv=None, target="probe"):
    """`make` run on the repository's Makefile from *tmp_path* (so its
    `include .env` reads *dotenv*), with a probe target built from init-db's
    own two expressions, copied from the Makefile: psql's `PGPASSWORD` and the
    runner's `DATABASE_URL`. It prints the password psql would be given and
    libpq's parse of the URL the runner would be given."""
    text = (ROOT / "Makefile").read_text()
    url_expr = re.search(
        r'DATABASE_URL="(.*?)" python -m scripts\.migration_runner apply', text
    )
    pg_expr = re.search(
        r'PGPASSWORD="(.*?)" psql \$\(PG_OPTS\) -d \$\(DB_NAME\) -q', text
    )
    assert url_expr is not None and pg_expr is not None, "init-db changed shape"
    show = tmp_path / "show.py"
    show.write_text(
        "import json, os\n"
        "from psycopg2.extensions import parse_dsn\n"
        "print(json.dumps({'psql': os.environ['PGPASSWORD'],"
        " 'runner': parse_dsn(os.environ['DATABASE_URL'])}))\n"
    )
    fields = tmp_path / "fields.py"
    fields.write_text(
        "import json, os\n"
        "print(json.dumps(sorted(k for k in os.environ if k.startswith('DB_'))))\n"
    )
    probe = tmp_path / "probe.mk"
    probe.write_text(
        f'probe:\n\t@PGPASSWORD="{pg_expr.group(1)}" '
        f'DATABASE_URL="{url_expr.group(1)}" python {show}\n'
        f"fields:\n\t@python {fields}\n"
    )
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv)
    run_env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "PYTHONPATH": str(ROOT),
        "HOME": str(tmp_path),
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


@needs_make
def test_make_hands_the_runner_a_hostile_command_line_intact(tmp_path):
    got = _through_make(
        tmp_path,
        args=(
            f"DB_USER={HOSTILE_USER}",
            f"DB_PASSWORD={HOSTILE_PASSWORD}",
            f"DB_HOST={SOCKET_DIR}",
            "DB_PORT=6543",
            f"DB_NAME={HOSTILE_NAME}",
        ),
    )
    assert got["psql"] == HOSTILE_PASSWORD
    assert got["runner"] == {
        "user": HOSTILE_USER,
        "password": HOSTILE_PASSWORD,
        "host": SOCKET_DIR,
        "port": "6543",
        "dbname": HOSTILE_NAME,
    }


# How a `.env` may spell the password, and what the shell makes of each in
# psql's double-quoted `PGPASSWORD` — and so, now, in the runner's step. The shell's
# answer is not always the value meant (single quotes are literal inside double
# quotes; a comment's leading space stays): that is every psql recipe's limit,
# recorded here, and the property pinned is that the two steps agree. Built
# from AT_VALUE, never a literal.
DOTENV_SPELLINGS = {
    "bare": (AT_VALUE, AT_VALUE),
    "double-quoted": (f'"{AT_VALUE}"', AT_VALUE),
    "single-quoted": (f"'{AT_VALUE}'", f"'{AT_VALUE}'"),
    "escaped quote inside": ('"ab\\"cd"', 'ab"cd'),
    "trailing comment": (f'"{AT_VALUE}" # a local note', f"{AT_VALUE} "),
}


@needs_make
@pytest.mark.parametrize("spelling", sorted(DOTENV_SPELLINGS))
def test_the_runner_connects_with_what_psql_does(tmp_path, spelling):
    written, expected = DOTENV_SPELLINGS[spelling]
    got = _through_make(tmp_path, dotenv=f"DB_USER=dev\nDB_PASSWORD={written}\n")
    assert got["psql"] == expected
    assert got["runner"].get("password") == got["psql"], got


@needs_make
def test_a_dollar_is_expanded_in_both_steps_alike(tmp_path):
    """make turns `$$` into `$`, then the shell line expands `$cd` — in psql's
    step and the runner's alike. A password carrying `$` does not survive
    `make init-db` at all; what is pinned is that neither step disagrees."""
    got = _through_make(
        tmp_path, args=("DB_USER=dev", f"DB_PASSWORD={DOLLAR_ON_THE_COMMAND_LINE}")
    )
    assert got["psql"] == "ab"
    assert got["runner"]["password"] == got["psql"]


@needs_make
def test_make_with_nothing_set_hands_the_runner_the_defaults(tmp_path):
    got = _through_make(tmp_path)
    assert got["psql"] == ""
    assert got["runner"] == {"host": "localhost", "port": "5432", "dbname": "storydump"}


@needs_make
def test_without_a_dotenv_no_db_field_reaches_another_target(tmp_path):
    assert _through_make(tmp_path, target="fields") == []
