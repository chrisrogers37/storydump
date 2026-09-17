"""The legacy tier is gone — the packages, their importers, their closure,
their ratchet segment (plan ``2026-09-16-legacy-tear-out``, phase 01; #1216).

Deleting a package is one commit. What keeps it deleted is a predicate: no
module under ``src``, ``scripts``, ``storydump_cli`` or ``tests`` imports a
name the deletion removed — read from the AST, walked, so an import inside a
function counts (``media_sources/factory.py`` hid one at ``:149``); the three
deployed entrypoints pull no such module into their closure in a fresh
interpreter; and the FC-2 ratchet's baseline reads the target tier only. The
same predicate, run over ``tests/``, is the rule the deletion of the test
files followed: a file whose AST imports a deleted module goes.

The predicate has a positive control (a planted importer of every forbidden
prefix is found) because a scanner that reads nothing is green for the wrong
reason — the failure mode ``tests/test_legacy_cli_gone.py`` guards against
one tier down.
"""

from __future__ import annotations

import ast
import json
import os
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

#: `src.models` itself exports nothing any more; the target models are
#: imported by their own path, `src.models.target...`.
TARGET_MODELS = "src.models.target"

SCANNED_ROOTS = ("src", "scripts", "storydump_cli", "tests")

#: The Telegram adapters the target tier keeps — the whole `telegram_modules`
#: axis of the FC-2 ratchet once the legacy segment is empty.
TARGET_TELEGRAM_MODULES = [
    "src/channels/telegram_transport.py",
    "src/channels/telegram_webhook_registration.py",
    "src/exceptions/telegram.py",
    "src/services/target/telegram_dispatch.py",
]

ENTRYPOINTS = ("src.main", "src.api.app", "src.worker")


def forbidden(name: str) -> bool:
    if name == "src.models" or (
        name.startswith("src.models.") and not name.startswith(TARGET_MODELS)
    ):
        return True
    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PREFIXES)


def imported_names(tree: ast.AST):
    """Every module name an import statement in *tree* can resolve to, at any
    depth — `import a.b`, `from a import b` (as `a` and `a.b`), walked."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module
            for alias in node.names:
                yield f"{node.module}.{alias.name}"


def legacy_importers(root: Path, roots=SCANNED_ROOTS) -> list[str]:
    """`path:line name` for every import of a forbidden module under *roots*."""
    found = []
    for top in roots:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - not this gate's concern
                continue
            for node in ast.walk(tree):
                names = (
                    list(imported_names(node))
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    else []
                )
                for name in names:
                    if forbidden(name):
                        rel = path.relative_to(root).as_posix()
                        found.append(f"{rel}:{node.lineno} {name}")
                        break
    return found


def legacy_importing_files(root: Path, roots=SCANNED_ROOTS) -> list[str]:
    """The files the deletion rule names: those with at least one hit."""
    return sorted({hit.split(":", 1)[0] for hit in legacy_importers(root, roots)})


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
    """Positive control: a scanner that reads nothing is green for the wrong
    reason. Every forbidden prefix is planted, at function depth (the shape
    `ast.walk` exists for), and reported."""

    @pytest.mark.parametrize("prefix", FORBIDDEN_PREFIXES + ("src.models",))
    def test_a_planted_importer_is_found(self, tmp_path, prefix):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "m.py").write_text(
            f"def f():\n    import {prefix}\n    return {prefix}\n"
        )
        assert legacy_importers(tmp_path) == [f"src/m.py:2 {prefix}"]

    def test_a_from_import_of_a_deleted_submodule_is_found(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "m.py").write_text("from src.utils import validators\n")
        assert legacy_importers(tmp_path) == ["src/m.py:1 src.utils.validators"]

    def test_the_target_models_are_not_flagged(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "m.py").write_text(
            "from src.models.target import TargetBase\n"
            "from src.exceptions.tenancy import TenantResolutionError\n"
        )
        assert legacy_importers(tmp_path) == []

    def test_a_deeper_import_under_a_deleted_package_is_found(self, tmp_path):
        """`import src.services.core.loops.guarded` names no forbidden prefix
        exactly; the prefix arm of `forbidden()` is what catches it."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "m.py").write_text(
            "import src.services.core.loops.guarded\n"
        )
        assert legacy_importers(tmp_path) == [
            "src/m.py:1 src.services.core.loops.guarded"
        ]

    def test_the_forbidden_set_names_every_deleted_module(self):
        """The two lists are one fact: a path the deletion removed must be a
        module name the predicate refuses."""
        for path in DELETED_PACKAGES + DELETED_FILES:
            name = path.removesuffix(".py").replace("/", ".")
            assert forbidden(name), f"{path} was deleted but {name} is not forbidden"

    def test_the_real_tree_was_scanned(self):
        scanned = [p for top in SCANNED_ROOTS for p in (ROOT / top).rglob("*.py")]
        assert len(scanned) > 200, "the scan reached almost nothing"


# --- the closure ------------------------------------------------------------------


def test_the_deployed_entrypoints_pull_no_legacy_module():
    """Import-reachability, measured where it is honest: a fresh interpreter.
    In the shared pytest process the answer is whatever earlier files left in
    `sys.modules`. The environment is this run's own — CI's and the recipes'
    dummy Telegram values included (their retirement is phase 02's)."""
    code = (
        "import sys\n"
        f"import {', '.join(ENTRYPOINTS)}\n"
        f"prefixes = {FORBIDDEN_PREFIXES!r}\n"
        "def forbidden(name):\n"
        "    if name == 'src.models' or (name.startswith('src.models.')"
        f" and not name.startswith({TARGET_MODELS!r})):\n"
        "        return True\n"
        "    return any(name == p or name.startswith(p + '.') for p in prefixes)\n"
        "assert 'src.worker' in sys.modules, 'the worker root is not in the closure'\n"
        "print(sorted(m for m in sys.modules if forbidden(m)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=dict(os.environ),
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    # the app logs to stdout at import (an unset TARGET_DATABASE_URL warns);
    # the answer is the last line
    answer = proc.stdout.strip().splitlines()[-1]
    assert answer == "[]", f"legacy modules in the deployed closure: {answer}"


# --- the ratchet and the dependencies ----------------------------------------------


def test_the_fc2_ratchet_reads_the_target_tier_only():
    baseline = json.loads(
        (ROOT / "scripts" / "telegram_ratchet_baseline.json").read_text()
    )
    assert baseline["core_telegram_modules"] == [], (
        "FC-2 clause 3: the core segment must read empty"
    )
    assert baseline["telegram_modules"] == TARGET_TELEGRAM_MODULES
    assert baseline["chat_id_functions_outside_adapters"] == []
    assert baseline["provider_account_ref_log_sites"] == []


@pytest.mark.parametrize("dependency", ["python-telegram-bot", "Pillow"])
def test_a_dependency_only_the_legacy_tier_used_is_gone(dependency):
    for name in ("requirements.txt", "setup.py"):
        assert dependency not in (ROOT / name).read_text(), f"{dependency} in {name}"
