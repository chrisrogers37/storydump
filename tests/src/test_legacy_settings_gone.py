"""The legacy settings, the worker switch and the config surfaces are retired
(plan ``2026-09-16-legacy-tear-out``, phase 02; #1222, #1205; fork F5).

Every `Settings` field the deleted legacy tier read — the three bare Telegram
variables it REQUIRED of every process, and every other field nothing reads —
is gone, so a process needs only the variables the target tier reads; no
field is required any more; the deployed entrypoints import with no variable
of either family in the environment; the worker switch and its contract module
are gone; `.env.example` names exactly what the tree reads; CI, the Makefile
and the guides set no variable nothing reads.

The rule the deletion followed is measured here rather than trusted: a field
survives only while something READS it off the settings object — an attribute
read, a `getattr`, a named-setting lookup, or a property that itself has such
a reader. A comment, a docstring, or an `env.get("NAME")` on the process
environment is not a read of the field (round 1 of the review found the first
version of this rule accepting the sentence "deliberately NOT read from
`settings.DB_MAX_OVERFLOW`" as that field's reader).

A KNOWN LIMIT, stated rather than hidden: the rule is syntactic. A read in a
branch no caller takes still counts — `DB_NAME`'s one reader is
`async_database_url`'s `database or settings.DB_NAME`, and every caller passes
`database` (the re-verify of round 1 found it). The field stays because `make`
reads the variable of the same name; the rule cannot see that either way.

The landing app (`landing/src/lib/telegram.ts`) reads `TELEGRAM_BOT_TOKEN` and
`ADMIN_TELEGRAM_CHAT_ID` from ITS OWN environment on Vercel — a different
consumer, untouched here; the retirement is the Python settings and the
Railway variables.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = ROOT / "src" / "config" / "settings.py"

#: The three the legacy tier required of every process (#1222).
REQUIRED_BY_THE_LEGACY_TIER = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHANNEL_ID",
    "ADMIN_TELEGRAM_CHAT_ID",
)

#: Every field the phase deleted, each measured at ZERO reads off the settings
#: object on 2026-09-17/18 (the AST rule below; the ledger carries the table).
RETIRED_FIELDS = REQUIRED_BY_THE_LEGACY_TIER + (
    "TELEGRAM_MAX_CONCURRENT_UPDATES",
    "TELEGRAM_RATE_LIMITER_ENABLED",
    "TELEGRAM_RATE_LIMITER_MAX_RETRIES",
    "MEDIA_DIR",
    "BACKUP_DIR",
    "BACKUP_RETENTION_DAYS",
    "FACEBOOK_APP_ID",
    "GOOGLE_REFRESH_TOKEN_TTL_DAYS",
    "CLOUD_STORAGE_PROVIDER",
    "CLOUD_UPLOAD_RETENTION_HOURS",
    "CLOUD_UPLOAD_TIMEOUT_SECONDS",
    "INSTAGRAM_PUBLISH_LIMIT_FALLBACK",
    "MEDIA_SYNC_INTERVAL_SECONDS",
    "ANTHROPIC_API_KEY",
    "CAPTION_MODEL",
    "META_GRAPH_API_VERSION",
    # round 1 of the review: the first rule let these six through
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "DATABASE_URL",
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
)

#: Retired as FIELDS only: the process environment still carries them — the
#: migration runner reads `DATABASE_URL`, the worker's transit seam reads the
#: Cloudinary trio (`src/worker.py::_transit_from_env`).
STILL_READ_FROM_THE_ENVIRONMENT = frozenset(
    {
        "DATABASE_URL",
        "CLOUDINARY_CLOUD_NAME",
        "CLOUDINARY_API_KEY",
        "CLOUDINARY_API_SECRET",
    }
)

#: Names the legacy `.env.example` and the guides told a reader to set, which
#: were never `Settings` fields at the base or died with the tier.
LEGACY_ENV_NAMES = (
    "ENABLE_INSTAGRAM_API",
    "POSTS_PER_DAY",
    "POSTING_HOURS_START",
    "POSTING_HOURS_END",
    "REPOST_TTL_DAYS",
    "MEDIA_SOURCE_TYPE",
    "MEDIA_SOURCE_ROOT",
    "MEDIA_SYNC_ENABLED",
    "INSTAGRAM_ACCOUNT_ID",
    "INSTAGRAM_ACCESS_TOKEN",
    "DRY_RUN_MODE",
    "SEND_LIFECYCLE_NOTIFICATIONS",
    "INSTAGRAM_USERNAME",
    "CAPTION_STYLE",
    "INSTAGRAM_DEEPLINK_URL",
)

#: The worker switch (#942) and its contract module.
WORKER_SWITCH = "WORKER_IMPL"

#: Every variable NOTHING reads: what no setter may name.
DEAD_VARIABLES = tuple(
    sorted(
        (set(RETIRED_FIELDS) | set(LEGACY_ENV_NAMES) | {WORKER_SWITCH})
        - STILL_READ_FROM_THE_ENVIRONMENT
    )
)

CODE_ROOTS = ("src", "scripts", "storydump_cli")
SENTINEL = "IMPORTED-WITHOUT-ANY-TIER-ENV="

#: Where a deploy or a developer sets variables: none may name a dead one.
SETTERS = (
    ".env.example",
    ".github/workflows/ci.yml",
    ".github/workflows/schema-drift.yml",
    "Makefile",
    "AGENTS.md",
    "README.md",
    "documentation/guides/testing-guide.md",
    "documentation/guides/dev-environment-setup.md",
    "documentation/guides/deployment.md",
    "documentation/guides/cloud-deployment.md",
    "documentation/guides/ci-cd-pipeline.md",
)

#: The names a settings object travels under: `from … import settings` (the
#: only spelling in the tree today, 44 sites) and `as _settings`, accepted so an
#: aliased import can never hide a read from the rule.
SETTINGS_RECEIVERS = frozenset({"settings", "_settings"})


def _fields() -> dict:
    from src.config.settings import Settings

    return Settings.model_fields


def _code_files() -> list[Path]:
    return [
        p for top in CODE_ROOTS for p in (ROOT / top).rglob("*.py") if p != SETTINGS
    ]


def _harness_files() -> list[Path]:
    """The test harness is a real consumer (it finds its database through
    `DB_*` and `TEST_DB_NAME`) — but the settings' OWN unit tests and this
    guard are not: a field may not keep itself alive by being tested."""
    own_dir = ROOT / "tests" / "src" / "config"
    this = Path(__file__).resolve()
    return [
        p
        for p in (ROOT / "tests").rglob("*.py")
        if own_dir not in p.parents and p.resolve() != this
    ]


def _code_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _code_files())


def settings_reads(files) -> set[str]:
    """Every name READ off a settings object in *files*, by AST — so a comment
    or a docstring can never count:

    - `settings.NAME` / `_settings.NAME` (an attribute read on a receiver);
    - `getattr(settings, "NAME", …)`;
    - a named-setting lookup, the OAuth clients' idiom: a string passed as a
      `*_setting=` keyword or bound to a `*_SETTING` constant, which
      `oauth_client.configured` resolves with `getattr(settings, name)`.
    """
    found: set[str] = set()
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not this gate's concern
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in SETTINGS_RECEIVERS
            ):
                found.add(node.attr)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in SETTINGS_RECEIVERS
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                found.add(node.args[1].value)
            elif (
                isinstance(node, ast.keyword)
                and (node.arg or "").endswith("_setting")
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.add(node.value.value)
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.endswith("_SETTING")
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.add(node.value.value)
    return found


def fields_read_through_a_property(read: set[str]) -> set[str]:
    """Fields a settings PROPERTY reads via `self.NAME` — counted only when the
    property itself is in *read* (a property nobody calls keeps nothing
    alive: `META_GRAPH_API_VERSION` and `DATABASE_URL` both hid that way)."""
    tree = ast.parse(SETTINGS.read_text(encoding="utf-8"))
    out: set[str] = set()
    for cls in ast.walk(tree):
        if not (isinstance(cls, ast.ClassDef) and cls.name == "Settings"):
            continue
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name not in read:
                continue
            out |= {
                a.attr
                for a in ast.walk(fn)
                if isinstance(a, ast.Attribute)
                and isinstance(a.value, ast.Name)
                and a.value.id == "self"
            }
    return out


def unread_fields(fields, read: set[str]) -> list[str]:
    alive = read | fields_read_through_a_property(read)
    return [name for name in fields if name not in alive]


def env_names_the_tree_reads() -> set[str]:
    """Every environment variable the code reads OUTSIDE `Settings`: the
    `environ.get("X")` / `env.get("X")` / `getenv("X")` / `environ["X"]`
    calls, the `*_VAR = "X"` / `*_ENV = "X"` constants the Telegram
    registration, the worker and the vocabulary read through, and every
    `TARGET_*` name spelled as a literal (the worker's lane knobs live in a
    dict of names)."""
    text = _code_text()
    names = set(re.findall(r"(?:environ|env)\.get\(\s*\"([A-Z][A-Z0-9_]*)\"", text))
    names |= set(re.findall(r"getenv\(\s*\"([A-Z][A-Z0-9_]*)\"", text))
    names |= set(re.findall(r"environ\[\s*\"([A-Z][A-Z0-9_]*)\"\s*\]", text))
    names |= set(re.findall(r"_(?:VAR|ENV)\s*=\s*\"([A-Z][A-Z0-9_]*)\"", text))
    names |= set(re.findall(r"\"(TARGET_[A-Z0-9_]+)\"", text))
    return names


# --- the fields ------------------------------------------------------------------


@pytest.mark.parametrize("name", RETIRED_FIELDS)
def test_the_retired_field_is_gone(name):
    assert name not in _fields(), f"{name} is still a Settings field"


def test_no_field_is_required_any_more():
    required = [n for n, f in _fields().items() if f.is_required()]
    assert required == [], (
        f"required fields: {required} — a process must not need a variable the"
        " target tier does not read"
    )


def test_every_surviving_field_has_a_reader():
    """The rule, measured on the real tree: deployed code or the harness reads
    every field off the settings object, directly or through a property that
    is itself read."""
    read = settings_reads(_code_files()) | settings_reads(_harness_files())
    assert unread_fields(_fields(), read) == [], (
        "fields nothing reads off the settings object — delete them, or read them"
    )


class TestTheReaderRuleCanActuallySee:
    """The rule's positive and negative controls, on planted source: a gate
    that accepts a comment as a reader is the defect round 1 found."""

    def _reads(self, tmp_path, src: str) -> set[str]:
        path = tmp_path / "m.py"
        path.write_text(src, encoding="utf-8")
        return settings_reads([path])

    def test_a_comment_or_a_docstring_is_not_a_read(self, tmp_path):
        src = (
            '"""This is deliberately NOT read from `settings.DB_MAX_OVERFLOW`."""\n'
            "# see settings.DB_POOL_SIZE\n"
            "X = 1\n"
        )
        assert self._reads(tmp_path, src) == set()

    def test_an_environment_read_is_not_a_read_of_the_field(self, tmp_path):
        assert self._reads(tmp_path, 'v = env.get("CLOUDINARY_API_KEY")\n') == set()

    def test_the_three_ways_a_field_is_read(self, tmp_path):
        src = (
            "a = settings.LOG_LEVEL\n"
            'b = getattr(settings, "ENCRYPTION_KEYS", None)\n'
            'c = configured(id_setting="INSTAGRAM_APP_ID")\n'
            'BASE_SETTING = "OAUTH_REDIRECT_BASE_URL"\n'
            "d = _settings.web_app_origin\n"
        )
        assert self._reads(tmp_path, src) == {
            "LOG_LEVEL",
            "ENCRYPTION_KEYS",
            "INSTAGRAM_APP_ID",
            "OAUTH_REDIRECT_BASE_URL",
            "web_app_origin",
        }

    def test_an_attribute_of_the_same_name_on_something_else_is_not_a_read(
        self, tmp_path
    ):
        """`args.database_url` in the migration runner is argparse's, and the
        first rule's text match took it for six callers of the property."""
        assert self._reads(tmp_path, "u = args.database_url\n") == set()

    def test_a_property_nobody_calls_keeps_no_field_alive(self):
        assert "WEB_APP_URL" in fields_read_through_a_property({"web_app_origin"})
        assert fields_read_through_a_property(set()) == set()


def test_settings_construct_with_no_legacy_variable(monkeypatch):
    from src.config.settings import Settings

    for name in REQUIRED_BY_THE_LEGACY_TIER + (WORKER_SWITCH,):
        monkeypatch.delenv(name, raising=False)
    Settings(_env_file=None)  # a raise is the failure


# --- the entrypoints and the switch ---------------------------------------------------


def test_the_deployed_entrypoints_import_with_no_variable_of_either_tier(tmp_path):
    """A fresh interpreter with every `TELEGRAM*` and `TARGET_*` variable and
    the worker switch stripped, run from an EMPTY directory so no developer's
    `.env` can satisfy a requirement on the quiet: the three deployed
    entrypoints import. The `TARGET_*` family is read at run time, never at
    import."""
    env = {
        k: v
        for k, v in os.environ.items()
        if "TELEGRAM" not in k and not k.startswith("TARGET_") and k != WORKER_SWITCH
    }
    env["PYTHONPATH"] = str(ROOT)
    code = f"import src.main, src.api.app, src.worker\nprint({SENTINEL!r} + 'ok')\n"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert f"{SENTINEL}ok" in proc.stdout.splitlines()


def test_the_worker_switch_and_its_contract_module_are_gone():
    assert not (ROOT / "src" / "worker_impl.py").exists()
    hits = [
        f"{p.relative_to(ROOT)}:{i}"
        for top in CODE_ROOTS
        for p in (ROOT / top).rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if WORKER_SWITCH in line
    ]
    assert hits == [], f"the worker switch is still named: {hits}"


def _nothing_past_the_refusal_may_run(monkeypatch, worker) -> None:
    """THE TEST'S OWN SAFETY (round 1 of the review): under the regression
    these tests exist to catch — a fallback URL comes back — the real
    `worker.main()` would go on to build an engine and a transport and call
    `asyncio.run(run(app))`: a live worker inside pytest. Everything past the
    refusal fails the test instead of running."""

    def refuse(*_args, **_kwargs):
        pytest.fail("the worker went past its refusal and tried to boot")

    monkeypatch.setattr(worker.unit_of_work, "create_engine", refuse)
    monkeypatch.setattr(worker, "compose", refuse)
    monkeypatch.setattr(worker.asyncio, "run", refuse)


def test_the_worker_refuses_to_boot_without_its_database_url(monkeypatch, capsys):
    """With the legacy loops deleted, a worker booted without
    `TARGET_DATABASE_URL` would otherwise run the target root against the
    `DB_*`-configured database (the round-1 lens of phase 01). It refuses
    with exit 2, naming the variable — the API already refused its way
    (`_engine_from_env`: no engine, 503 on every data route)."""
    import src.worker as worker

    _nothing_past_the_refusal_may_run(monkeypatch, worker)
    monkeypatch.delenv("TARGET_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        worker.main()
    assert exc.value.code == 2
    assert "TARGET_DATABASE_URL" in capsys.readouterr().err


def test_a_blank_database_url_is_refused_by_name_too(monkeypatch, capsys):
    """Present-but-blank is a state a dashboard row saved empty produces: it
    must meet the same named refusal, not a traceback out of `create_engine`."""
    import src.worker as worker

    _nothing_past_the_refusal_may_run(monkeypatch, worker)
    monkeypatch.setenv("TARGET_DATABASE_URL", "   ")
    with pytest.raises(SystemExit) as exc:
        worker.main()
    assert exc.value.code == 2
    assert "TARGET_DATABASE_URL" in capsys.readouterr().err


def test_the_api_treats_a_blank_database_url_as_absent():
    """The API's half of the same absence: no engine, 503 on every data route,
    `/health` still answering — never a `create_engine("   ")` at import."""
    from src.api.app import _engine_from_env

    assert _engine_from_env({"TARGET_DATABASE_URL": "   "}) is None
    assert _engine_from_env({}) is None


def test_create_engine_takes_no_settings_built_fallback():
    from src.services.target import unit_of_work

    with pytest.raises(ValueError, match="TARGET_DATABASE_URL"):
        unit_of_work.create_engine(None)


# --- the surfaces that set variables ---------------------------------------------------


def _assignments(text: str) -> set[str]:
    """`NAME=` at a line's start, commented out or not — a variable the file
    tells a reader to set."""
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))


#: The environment read outside `Settings`, measured on 2026-09-17 after the
#: deletion and pinned by EQUALITY (the plan's rule): a new run-time read is a
#: visible line in a diff, and a name that stops being read is one too.
ENV_READ_OUTSIDE_SETTINGS = {
    # the deployed roots' database and the runner's
    "TARGET_DATABASE_URL",
    "DATABASE_URL",
    # the one bot, its webhook and the harness's Telegram double
    "TARGET_TELEGRAM_BOT_TOKEN",
    "TARGET_TELEGRAM_BOT_USERNAME",
    "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN",
    "TARGET_TELEGRAM_WEBHOOK_URL",
    "TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER",
    "TARGET_TELEGRAM_WEBHOOK_MAX_CONNECTIONS",
    "TARGET_TELEGRAM_API_BASE",
    # the worker's knobs
    "TARGET_WORKER_INTERACTIVE_CONCURRENCY",
    "TARGET_WORKER_BULK_CONCURRENCY",
    "TARGET_USAGE_PRECHECK_ENABLED",
    "WORKER_LOG_LEVEL",
    # the processes' ports and the transit store
    "PORT",
    "WEB_CONCURRENCY",
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
    "META_GRAPH_VERSION",
    # the notification sender and Railway's own marker. The two Railway account
    # tokens left this set with the legacy tier's measurement instruments
    # (#1216 — the CHANGELOG names them), which held their only reader; the CLI
    # has never read one, it uses the `railway` login.
    "RESEND_API_KEY",
    "EMAIL_FROM",
    "RAILWAY_ENVIRONMENT_NAME",
    # the storydump CLI's own (a client; never a service's)
    "STORYDUMP_TOKEN",
    "STORYDUMP_API",
    "STORYDUMP_CONFIG_DIR",
    "STORYDUMP_INSECURE_HTTP",
}


def test_the_environment_read_outside_settings_is_pinned():
    assert env_names_the_tree_reads() == ENV_READ_OUTSIDE_SETTINGS, (
        "the set of environment variables read outside Settings changed —"
        " add the name here AND to .env.example, or drop it from both"
    )


def test_env_example_names_exactly_what_the_tree_reads():
    """EXACT agreement, both directions: a name nothing reads is a lie the
    next reader has to disprove, and a name the tree reads that the example
    omits is a variable nobody knows to set."""
    named = _assignments((ROOT / ".env.example").read_text())
    known = set(_fields()) | env_names_the_tree_reads()
    assert sorted(named - known) == [], ".env.example names variables nothing reads"
    assert sorted(known - named) == [], ".env.example omits variables the tree reads"


def test_the_dead_list_names_nothing_the_tree_reads():
    """The list of dead variables cannot rot into naming a live one."""
    live = set(_fields()) | env_names_the_tree_reads()
    assert sorted(set(DEAD_VARIABLES) & live) == []
    assert STILL_READ_FROM_THE_ENVIRONMENT <= env_names_the_tree_reads()


@pytest.mark.parametrize("path", SETTERS)
def test_no_setter_names_a_dead_variable(path):
    text = (ROOT / path).read_text()
    dead = [
        name
        for name in DEAD_VARIABLES
        if re.search(rf"(^|[^A-Z_]){name}([^A-Z_]|$)", text, re.M)
    ]
    assert dead == [], f"{path} still names {dead}"


def test_the_batteries_recipes_set_no_dead_variable():
    """The RECIPE lines (`UNIT=` / `GATE=`), not the whole file: a battery
    may name a dead variable as the mutation it plants or the variable it
    unsets — what it may not do is set one for the tests to run under."""
    dead = re.compile(r"(^|[^A-Z_])(" + "|".join(DEAD_VARIABLES) + r")=")
    hits = [
        f"{p.name}:{i}"
        for p in (ROOT / "tests" / "mutations").glob("*.sh")
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if line.startswith(("UNIT=", "GATE=")) and dead.search(line)
    ]
    assert hits == [], f"batteries still setting a dead variable: {hits}"


# --- the Makefile ------------------------------------------------------------------


def _recipe(target: str) -> str:
    """The recipe lines of one Makefile target."""
    lines = (ROOT / "Makefile").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    body = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(("\t", " ")):
            break
        body.append(line)
    return "\n".join(body)


def test_every_file_the_makefile_feeds_psql_exists():
    """`init-db` once named a fixture that lived on another branch, and four
    targets failed where the CHANGELOG said they ran (round 1 of the review)."""
    text = (ROOT / "Makefile").read_text()
    named = re.findall(r"-f\s+([A-Za-z0-9_./-]+\.sql)", text)
    assert named, "the Makefile feeds psql no file — the pattern has rotted"
    missing = [f for f in named if not (ROOT / f).is_file()]
    assert missing == [], f"the Makefile names files that do not exist: {missing}"


def test_init_db_is_the_lanes_own_sequence():
    """Step 0 (the service roles, then the DDL door 050 calls), the by-hand
    base with the one table production made by hand (078 snapshots it, so a
    database built from the tree must hold it), then the runner — the order
    `run_bootstrap` and `run_lane` stand a world up in. Proven end to end on a
    throwaway PostgreSQL 15 (the ledger)."""
    recipe = _recipe("init-db")
    order = [
        recipe.index("scripts/window/step0_bootstrap.sql"),
        recipe.index("scripts/window/step0_legacy_ddl_door.sql"),
        recipe.index("scripts/setup_database.sql"),
        recipe.index("tests/scripts/fixtures/legacy_by_hand.sql"),
        recipe.index("scripts.migration_runner apply"),
    ]
    assert order == sorted(order), "init-db applies its files out of order"
    assert "ON_ERROR_STOP=1" in recipe


def test_the_makefile_validates_the_settings_that_exist_and_can_fail():
    recipe = _recipe("validate-env")
    assert "get_settings" not in recipe, "validate-env calls a function that is gone"
    assert "from src.config.settings import settings" in recipe
    assert "exit 1" in recipe, (
        "validate-env echoes its failure and exits 0 — a check that cannot fail"
    )


def test_make_install_installs_the_cli_extra():
    assert "'.[cli]'" in _recipe("install")


def test_make_dev_does_not_gate_a_local_worker_on_productions_health():
    assert "check-health" not in _recipe("dev")
