"""The legacy tier is gone — the packages, their importers, their closure,
their ratchet segment (plan ``2026-09-16-legacy-tear-out``, phase 01; #1216).

Deleting a package is one commit. What keeps it deleted is a predicate: no
module under ``src``, ``scripts``, ``storydump_cli`` or ``tests`` imports a
name the deletion removed — read from the AST, walked, so an import inside a
function counts (``media_sources/factory.py`` hid one at ``:149``), a relative
import is resolved against its package, and a module name passed to
``importlib.import_module`` or ``__import__`` as a literal counts too; the
deployed entrypoints (read from the Procfile, ``railway.toml`` and the console
script) pull no such module into their closure in a fresh interpreter; and the
FC-2 ratchet's baseline reads the target tier only. The same predicate, run
over ``tests/``, is the rule the deletion of the test files followed: a file
whose AST imports a deleted module goes.

The predicate has positive controls — a planted importer of every forbidden
prefix, at function depth, is found — because a scanner that reads nothing is
green for the wrong reason, the failure mode ``tests/test_legacy_cli_gone.py``
guards against one tier down.

**Out of scope, stated:** a module name assembled at runtime, a deleted class
imported by name from a surviving package (``from src.exceptions import
InstagramAPIError`` — collection fails on it, loudly), and a package re-created
under another name. The hand-kept lists below are the design's cost; the test
that compares them to each other is what keeps them one fact.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: The non-target model modules ``src/models/`` carried for the legacy schema.
LEGACY_MODELS = (
    "api_token",
    "audit_log",
    "category_mix",
    "chat_settings",
    "enums",
    "instagram_account",
    "media_item",
    "media_lock",
    "onboarding_session",
    "posting_history",
    "posting_queue",
    "service_run",
    "user_chat_membership",
    "user_interaction",
    "user",
)

DELETED_PACKAGES = (
    "src/services/core",
    "src/services/integrations",
    "src/services/media_sources",
    "src/services/domain",
    "src/repositories",
)

DELETED_FILES = (
    "src/services/base_service.py",
    "src/config/database.py",
    "src/utils/validators.py",
    "src/utils/file_hash.py",
    "src/utils/media_kind.py",
    "src/utils/resilience.py",
    "src/utils/image_processing.py",
    "src/utils/webapp_auth.py",
    "src/exceptions/backfill.py",
    "src/exceptions/google_drive.py",
    "src/exceptions/instagram.py",
    "scripts/init_db.py",
    "scripts/backfill_memberships.py",
) + tuple(f"src/models/{m}.py" for m in LEGACY_MODELS)

#: A module name is forbidden when it IS one of these or lives under one.
#: `src.models` itself stays (the target models live under `src.models.target`
#: and are imported by that path), so only the legacy model modules are listed.
FORBIDDEN_PREFIXES = (
    "src.services.core",
    "src.services.integrations",
    "src.services.media_sources",
    "src.services.domain",
    "src.services.base_service",
    "src.repositories",
    "src.config.database",
    "src.utils.validators",
    "src.utils.file_hash",
    "src.utils.media_kind",
    "src.utils.resilience",
    "src.utils.image_processing",
    "src.utils.webapp_auth",
    "src.exceptions.backfill",
    "src.exceptions.google_drive",
    "src.exceptions.instagram",
    "scripts.init_db",
    "scripts.backfill_memberships",
) + tuple(f"src.models.{m}" for m in LEGACY_MODELS)

SCANNED_ROOTS = ("src", "scripts", "storydump_cli", "tests")

#: Where a Telegram-named module may live once the legacy segment is empty.
TARGET_TELEGRAM_HOMES = ("src/channels/", "src/exceptions/", "src/services/target/")

#: What the deployed entrypoints are read from, and the one root they reach.
_PYTHON_M = re.compile(r"python -m ([\w.]+)")
_UVICORN = re.compile(r"uvicorn ([\w.]+):\w+")
_CONSOLE = re.compile(r'"\w+=([\w.]+):\w+"')
SENTINEL = "LEGACY-IN-CLOSURE="


def forbidden(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PREFIXES)


def deployed_entrypoints(root: Path = ROOT) -> tuple[str, ...]:
    """Every module a deploy or an install starts: `python -m X` in the
    Procfile and `railway.toml`, the ASGI app the Procfile hands uvicorn, the
    console script in `setup.py` — plus `src.worker`, the root `src.main`
    dispatches to. Derived, so a new entrypoint cannot be forgotten here."""
    found = set()
    for name in ("Procfile", "railway.toml"):
        text = (root / name).read_text()
        found.update(_PYTHON_M.findall(text))
        found.update(_UVICORN.findall(text))
    found.update(_CONSOLE.findall((root / "setup.py").read_text()))
    found.add("src.worker")
    return tuple(sorted(found))


def _package_of(path: Path, root: Path) -> list[str]:
    """`src/api/routes/x.py` → ["src", "api", "routes"]."""
    return list(path.relative_to(root).with_suffix("").parts)[:-1]


def _string_literal_imports(node: ast.AST):
    """`importlib.import_module("x")` and `__import__("x")` with a literal."""
    if not isinstance(node, ast.Call) or not node.args:
        return
    func = node.func
    if isinstance(func, ast.Name):
        target = func.id
    elif isinstance(func, ast.Attribute):
        target = func.attr
    else:
        return
    if target not in ("import_module", "__import__"):
        return
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        yield first.value


def imported_names(path: Path, root: Path):
    """`(lineno, module name)` for every import in *path*, at any depth:
    `import a.b`, `from a import b` (as `a` and `a.b`), a relative import
    resolved against the file's package, and a literal module name handed to
    `importlib.import_module`/`__import__`."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - not this gate's concern
        return
    package = _package_of(path, root)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            if base:
                yield node.lineno, base
            for alias in node.names:
                yield node.lineno, f"{base}.{alias.name}" if base else alias.name
        else:
            for name in _string_literal_imports(node):
                yield node.lineno, name


def legacy_importers(root: Path, roots=SCANNED_ROOTS) -> list[str]:
    """`path:line name` — one entry per import statement of a forbidden module
    under *roots* (a statement naming several is reported once)."""
    found = []
    for top in roots:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            seen_lines = set()
            for lineno, name in imported_names(path, root):
                if forbidden(name) and lineno not in seen_lines:
                    seen_lines.add(lineno)
                    rel = path.relative_to(root).as_posix()
                    found.append(f"{rel}:{lineno} {name}")
    return found


def legacy_importing_files(root: Path, roots=SCANNED_ROOTS) -> list[str]:
    """The files the deletion rule names: those with at least one hit."""
    return sorted({hit.split(":", 1)[0] for hit in legacy_importers(root, roots)})


def _normalized_requirements(text: str) -> set[str]:
    """PEP 503 names from a requirements file or an `install_requires` list:
    lower-case, runs of `-_.` folded to `-`, extras and versions dropped."""
    names = set()
    for raw in re.findall(r'^\s*"?([A-Za-z0-9][A-Za-z0-9._-]*)', text, re.M):
        names.add(re.sub(r"[-_.]+", "-", raw).lower())
    return names


# --- the deletion ---------------------------------------------------------------


@pytest.mark.parametrize("package", DELETED_PACKAGES)
def test_the_legacy_package_is_gone(package):
    """No source file survives — a stray `__pycache__` is not a package."""
    left = sorted((ROOT / package).rglob("*.py")) if (ROOT / package).exists() else []
    assert left == [], f"{package} still holds {len(left)} module(s): {left[:3]}"


@pytest.mark.parametrize("path", DELETED_FILES)
def test_the_legacy_module_is_gone(path):
    assert not (ROOT / path).exists(), f"{path} is still here"


def test_the_models_package_exports_nothing():
    """`src/models/__init__.py` re-exported fourteen legacy models; the
    target models are imported by their own path."""
    tree = ast.parse((ROOT / "src" / "models" / "__init__.py").read_text())
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert imports == [], "src/models/__init__.py still imports a model"


# --- the importers ----------------------------------------------------------------


def test_nothing_in_the_tree_imports_a_deleted_module():
    hits = legacy_importers(ROOT)
    assert hits == [], (
        "these files import a module the tear-out deleted — delete the file"
        " with the code it tested, or move the import to the target tier:\n  "
        + "\n  ".join(hits)
    )


class TestThePredicateFindsWhatItIsFor:
    """Positive controls: a scanner that reads nothing is green for the wrong
    reason. The shapes the predicate claims to read are each planted and
    reported; the two hand-kept lists are compared to each other."""

    def _plant(self, tmp_path, rel, text):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_every_forbidden_prefix_planted_at_function_depth_is_found(self, tmp_path):
        body = "def f():\n" + "".join(f"    import {p}\n" for p in FORBIDDEN_PREFIXES)
        self._plant(tmp_path, "src/m.py", body)
        found = {hit.split(" ", 1)[1] for hit in legacy_importers(tmp_path)}
        assert found == set(FORBIDDEN_PREFIXES)

    def test_a_from_import_of_a_deleted_submodule_is_found(self, tmp_path):
        self._plant(tmp_path, "src/m.py", "from src.utils import validators\n")
        assert legacy_importers(tmp_path) == ["src/m.py:1 src.utils.validators"]

    def test_a_deeper_import_under_a_deleted_package_is_found(self, tmp_path):
        """`import src.services.core.loops.guarded` names no forbidden prefix
        exactly; the prefix arm of `forbidden()` is what catches it."""
        self._plant(tmp_path, "src/m.py", "import src.services.core.loops.guarded\n")
        assert legacy_importers(tmp_path) == [
            "src/m.py:1 src.services.core.loops.guarded"
        ]

    def test_a_relative_import_is_resolved_against_its_package(self, tmp_path):
        self._plant(tmp_path, "src/api/routes/m.py", "from ...repositories import x\n")
        assert legacy_importers(tmp_path) == ["src/api/routes/m.py:1 src.repositories"]

    @pytest.mark.parametrize(
        "call",
        [
            'importlib.import_module("src.repositories")',
            '__import__("src.services.core")',
        ],
    )
    def test_a_literal_module_name_handed_to_the_import_machinery_is_found(
        self, tmp_path, call
    ):
        self._plant(
            tmp_path, "src/m.py", f"import importlib\n\ndef f():\n    return {call}\n"
        )
        hits = legacy_importers(tmp_path)
        assert len(hits) == 1 and hits[0].startswith("src/m.py:4 "), hits

    def test_the_target_models_and_their_package_are_not_flagged(self, tmp_path):
        self._plant(
            tmp_path,
            "src/m.py",
            "import src.models.target\n"
            "from src.models import target\n"
            "from src.models.target import TargetBase\n"
            "from src.exceptions.tenancy import TenantResolutionError\n",
        )
        assert legacy_importers(tmp_path) == []

    def test_the_forbidden_set_names_every_deleted_module(self):
        """The two lists are one fact: a path the deletion removed must be a
        module name the predicate refuses."""
        for path in DELETED_PACKAGES + DELETED_FILES:
            name = path.removesuffix(".py").replace("/", ".")
            assert forbidden(name), f"{path} was deleted but {name} is not forbidden"

    def test_every_root_was_scanned(self):
        for top in SCANNED_ROOTS:
            files = list((ROOT / top).rglob("*.py"))
            assert len(files) > 5, f"{top}/ looks unscanned ({len(files)} files)"


# --- the closure ------------------------------------------------------------------


def test_the_entrypoints_are_read_from_where_a_deploy_reads_them():
    assert deployed_entrypoints() == (
        "scripts.migration_runner",
        "src.api.app",
        "src.main",
        "src.worker",
        "storydump_cli.main",
    )


def test_the_deployed_entrypoints_pull_no_legacy_module():
    """Import-reachability, measured where it is honest: a fresh interpreter.
    In the shared pytest process the answer is whatever earlier files left in
    `sys.modules`. The environment is this run's own — CI's and the recipes'
    dummy Telegram values included (their retirement is phase 02's). The
    answer is a sentinel line: the app logs to stdout at import."""
    code = (
        "import sys\n"
        f"import {', '.join(deployed_entrypoints())}\n"
        "from tests.src.test_legacy_tier_gone import forbidden\n"
        "assert 'src.worker' in sys.modules, 'the worker root is not in the closure'\n"
        f"print({SENTINEL!r} + repr(sorted(m for m in sys.modules if forbidden(m))))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    answers = [
        line[len(SENTINEL) :]
        for line in proc.stdout.splitlines()
        if line.startswith(SENTINEL)
    ]
    assert answers == ["[]"], f"legacy modules in the deployed closure: {answers}"


# --- the ratchet and the dependencies ----------------------------------------------


def test_the_fc2_ratchet_reads_the_target_tier_only():
    baseline = json.loads(
        (ROOT / "scripts" / "telegram_ratchet_baseline.json").read_text()
    )
    assert baseline["core_telegram_modules"] == [], (
        "FC-2 clause 3: the core segment must read empty"
    )
    assert baseline["chat_id_functions_outside_adapters"] == []
    assert baseline["provider_account_ref_log_sites"] == []
    modules = baseline["telegram_modules"]
    assert modules, "the target tier's own Telegram modules vanished from the axis"
    strays = [m for m in modules if not m.startswith(TARGET_TELEGRAM_HOMES)]
    assert strays == [], f"a Telegram-named module outside the target tier: {strays}"


@pytest.mark.parametrize("dependency", ["python-telegram-bot", "pillow"])
def test_a_dependency_only_the_legacy_tier_used_is_gone(dependency):
    """Compared as PEP 503 names, so `Pillow`, `pillow` and
    `python_telegram_bot[rate-limiter]` are all the same absence."""
    for name in ("requirements.txt", "setup.py"):
        names = _normalized_requirements((ROOT / name).read_text())
        assert dependency not in names, f"{dependency} in {name}"
