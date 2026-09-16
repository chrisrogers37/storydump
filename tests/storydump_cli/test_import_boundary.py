"""The CLI package reaches ``src`` only through the vocabulary module (spec §1).

Why a subprocess: by the time this test runs, ``tests/conftest.py`` has
already imported SQLAlchemy, psycopg2 and the settings module into this
interpreter, so ``sys.modules`` here proves nothing about the package. A
fresh interpreter that imports the package and nothing else is the only
honest witness. ``keyring`` is on the forbidden list too: it ships in the
``storydump[cli]`` extra and is imported lazily, so importing the package
must never need it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

MODULES = (
    "storydump_cli",
    "storydump_cli.main",
    "storydump_cli.client",
    "storydump_cli.storage",
    "storydump_cli.config",
    "storydump_cli.output",
    "storydump_cli.commands",
    "storydump_cli.commands.auth",
    "storydump_cli.commands.reads",
    "storydump_cli.commands.writes",
    "storydump_cli.commands.env",
    "storydump_cli.watch",
    "storydump_cli.railway",
    "storydump_cli.webhook",
)

FORBIDDEN_PACKAGES = {
    "sqlalchemy",
    "asyncpg",
    "psycopg2",
    "fastapi",
    "pydantic",
    "keyring",
}

ALLOWED_SRC = {
    "src",
    "src.services",
    "src.services.target",
    "src.services.target.vocabulary",
}


def _modules_loaded_by_importing_the_package() -> set[str]:
    script = (
        "import importlib, json, sys\n"
        f"for name in {MODULES!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


def test_the_package_imports_no_database_driver_or_framework():
    loaded = _modules_loaded_by_importing_the_package()
    top_level = {name.split(".")[0] for name in loaded}
    assert not top_level & FORBIDDEN_PACKAGES, sorted(top_level & FORBIDDEN_PACKAGES)


def test_no_import_anywhere_in_the_package_names_a_forbidden_package():
    """The subprocess test sees the import-time closure only; a lazy import
    inside a verb body (the shape `storage.py` uses for keyring) would pass
    it. Walk every import statement in every module instead."""
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parents[2] / "storydump_cli"
    # `keyring` is the one lazy import by design (F2: imported inside the
    # keychain backend so the package loads without it); a database driver
    # or the API framework is forbidden at any depth.
    never = FORBIDDEN_PACKAGES - {"keyring"}
    vocabulary = "src.services.target.vocabulary"
    offenders = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [f"{node.module}.{alias.name}" for alias in node.names]
            for name in names:
                top = name.split(".")[0]
                if top in never:
                    offenders.append(f"{path.name}:{node.lineno} {name}")
                if top == "src" and not name.startswith(vocabulary):
                    offenders.append(f"{path.name}:{node.lineno} {name}")
    assert offenders == [], offenders


def test_the_package_reaches_src_only_through_the_vocabulary():
    loaded = _modules_loaded_by_importing_the_package()
    from_src = {name for name in loaded if name == "src" or name.startswith("src.")}
    assert from_src == ALLOWED_SRC


#: The two fleet monitors `health` reuses — stdlib-only modules, the one
#: import from `scripts/` the boundary admits (plan 03 §Build notes).
ALLOWED_SCRIPTS = {"scripts", "scripts.posting_monitor", "scripts.scheduling_monitor"}
#: The third-party packages the CLI may import at any depth; `keyring` lazily.
ALLOWED_THIRD_PARTY = {"click", "httpx", "rich", "keyring"}


def test_the_package_reaches_scripts_only_for_the_two_monitors():
    loaded = _modules_loaded_by_importing_the_package()
    from_scripts = {n for n in loaded if n == "scripts" or n.startswith("scripts.")}
    assert from_scripts == ALLOWED_SCRIPTS


def test_the_two_monitors_import_the_standard_library_only():
    """The boundary lets them in because they are stdlib-only; this is what
    keeps them so."""
    import ast
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    for name in ("posting_monitor.py", "scheduling_monitor.py"):
        tree = ast.parse((scripts / name).read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for imported in names:
                top = imported.split(".")[0]
                assert top in sys.stdlib_module_names, f"{name} imports {imported}"


def test_every_direct_import_of_the_package_is_on_the_allowlist():
    """The exact boundary, at every depth: the standard library, the package
    itself, the vocabulary, the two monitors, and four third-party packages."""
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parents[2] / "storydump_cli"
    offenders = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [f"{node.module}.{alias.name}" for alias in node.names]
            for imported in names:
                top = imported.split(".")[0]
                ok = (
                    top in sys.stdlib_module_names
                    or top == "storydump_cli"
                    or imported == "src.services.target.vocabulary"
                    or imported.startswith("src.services.target.vocabulary.")
                    or imported in ALLOWED_SCRIPTS
                    or top in ALLOWED_THIRD_PARTY
                )
                if not ok:
                    offenders.append(f"{path.name}:{node.lineno} {imported}")
    assert offenders == [], offenders
