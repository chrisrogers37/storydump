import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_built_wheel_contains_migrations_manifest_and_ddl(tmp_path):
    pytest.importorskip("setuptools")
    pytest.importorskip("wheel")

    source = tmp_path / "source"
    wheel_dir = tmp_path / "wheels"
    source.mkdir()
    shutil.copy2(ROOT / "setup.py", source / "setup.py")
    for directory in ("src", "scripts", "storydump_cli"):
        shutil.copytree(
            ROOT / directory,
            source / directory,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    wheel_dir.mkdir()
    subprocess.run(
        [
            sys.executable,
            "setup.py",
            "bdist_wheel",
            "--dist-dir",
            str(wheel_dir),
        ],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )

    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    isolated_import = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from importlib.resources import files; "
        "p=files('scripts').joinpath('migrations'); "
        "assert p.joinpath('adoption_manifest.json').is_file(); "
        "assert len([item for item in p.iterdir() if item.name.endswith('.sql')]) == 7"
    )
    subprocess.run(
        [sys.executable, "-c", isolated_import, str(wheels[0])],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
    migration_files = {
        "scripts/migrations/000_initial.sql",
        "scripts/migrations/001_catalog_text.sql",
        "scripts/migrations/002_drop_archive_snapshots.sql",
        "scripts/migrations/003_instagram_past_posts.sql",
        "scripts/migrations/adoption_manifest.json",
    }
    assert migration_files <= names
    assert (
        len(
            [
                name
                for name in names
                if name.startswith("src/models/target/ddl/") and name.endswith(".sql")
            ]
        )
        == 9
    )
