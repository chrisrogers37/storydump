"""`requirements.txt` and `setup.py` name the same runtime dependencies.

The two files have different jobs and only their *names* have to agree:
`requirements.txt` pins exact versions for a deploy, `install_requires`
declares floors for `pip install -e .`. Nothing held the name sets equal, so
they drifted in both directions and the drift was invisible until something
ran. `install_requires` listed five packages no module imports (`alembic`,
the three `google-*`, `python-dateutil`) and omitted two the processes need:
`asyncpg`, without which the API and the worker cannot open a connection at
all, and `python-multipart`, without which FastAPI refuses the Meta `Form()`
routes at import time. A `pip install -e .` on its own therefore produced a
tree that could not start, and `pip install -r requirements.txt` hid it.

Held both ways, like `landing/.env.local.example` is held against the code
that reads it (`tests/test_landing_env_example.py`): every runtime pin is
declared, and every declaration is pinned. The next package to arrive lands
in both files or this fails naming it.

**Two exclusions, and they are the whole of the asymmetry.** The `# Testing`
block of `requirements.txt` is not runtime — it installs the test tier, which
a deploy does not get. `setup.py`'s `cli` extra is not runtime either: it is
opt-in (`pip install 'storydump[cli]'`), and decision F2 is that `keyring`
never ships to the API or the worker. Both exclusions are themselves pinned
below, so widening one is a visible edit rather than a quiet loosening.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
SETUP = ROOT / "setup.py"

#: The header that opens `requirements.txt`'s test tier. Everything from here
#: to the end of the file is excluded from the runtime set.
_TEST_HEADER = "# Testing"
#: What that block holds today. Pinned so that a test-only package added under
#: a different name is a deliberate edit here, not a silent exclusion.
TEST_TIER = frozenset({"pytest", "pytest-asyncio", "pytest-cov", "pytest-mock"})
#: `setup.py`'s opt-in extra. Its contents are not runtime dependencies.
CLI_EXTRA = "cli"

#: A requirement line: the name, up to the first version/extra/marker token.
_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize(name: str) -> str:
    """PEP 503: lower-case, runs of `-_.` folded to a single `-`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements_lines() -> list[str]:
    """Every non-comment, non-blank line of `requirements.txt`."""
    return [
        line.strip()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _split_at_test_tier() -> tuple[list[str], list[str]]:
    """The file's lines either side of the `# Testing` header."""
    text = REQUIREMENTS.read_text(encoding="utf-8")
    head, marker, tail = text.partition(f"{_TEST_HEADER}\n")
    assert marker, f"{_TEST_HEADER!r} is no longer a header in requirements.txt"

    def names(chunk: str) -> list[str]:
        out = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = _REQUIREMENT.match(line)
            assert match, f"unparsed requirement line: {line!r}"
            out.append(normalize(match.group(1)))
        return out

    return names(head), names(tail)


def requirement_names() -> set[str]:
    """The runtime pins — everything above the `# Testing` header."""
    runtime, _ = _split_at_test_tier()
    return set(runtime)


def _setup_call() -> ast.Call:
    tree = ast.parse(SETUP.read_text(encoding="utf-8"), filename=str(SETUP))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setup"
        ):
            return node
    raise AssertionError("no setup() call in setup.py")


def _keyword(name: str) -> ast.expr:
    for keyword in _setup_call().keywords:
        if keyword.arg == name:
            return keyword.value
    raise AssertionError(f"setup() has no {name}= argument")


def _names_from(node: ast.expr) -> set[str]:
    """The normalized distribution names of a list of requirement strings.

    Read by AST rather than by importing `setup.py`, which would execute it.
    """
    assert isinstance(node, ast.List), f"expected a list literal, got {type(node)}"
    names = set()
    for element in node.elts:
        assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
            f"a requirement that is not a string literal: {ast.dump(element)}"
        )
        match = _REQUIREMENT.match(element.value)
        assert match, f"unparsed requirement: {element.value!r}"
        names.add(normalize(match.group(1)))
    return names


def install_requires_names() -> set[str]:
    return _names_from(_keyword("install_requires"))


def extra_names(extra: str) -> set[str]:
    node = _keyword("extras_require")
    assert isinstance(node, ast.Dict), "extras_require is not a dict literal"
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == extra:
            return _names_from(value)
    raise AssertionError(f"extras_require has no {extra!r} key")


# --- the parsers see what they claim to ------------------------------------


def test_the_requirements_parser_finds_a_known_pin():
    """Positive control. `asyncpg` is the pin whose absence from
    `install_requires` was the defect this file exists to prevent."""
    assert "asyncpg" in requirement_names()


def test_the_setup_parser_finds_a_known_declaration():
    """Positive control, and it reads `setup.py` without executing it."""
    assert "sqlalchemy" in install_requires_names()


def test_the_excluded_test_tier_is_the_pytest_family():
    """The exclusion is narrow on purpose: if a test-only package that is not
    a `pytest*` arrives, this fails and the exclusion is widened on the
    record rather than by accident."""
    _, test_tier = _split_at_test_tier()
    assert set(test_tier) == {normalize(n) for n in TEST_TIER}, (
        f"the `{_TEST_HEADER}` block is now {sorted(test_tier)}"
    )


def test_the_test_tier_is_not_part_of_the_runtime_set():
    assert not requirement_names() & {normalize(n) for n in TEST_TIER}


# --- the two files agree, in both directions -------------------------------


def test_every_runtime_pin_is_declared_in_install_requires():
    missing = requirement_names() - install_requires_names()
    assert not missing, (
        "pinned in requirements.txt, absent from setup.py's install_requires "
        f"(a `pip install -e .` would not get them): {sorted(missing)}"
    )


def test_every_install_requires_name_is_pinned_in_requirements():
    unpinned = install_requires_names() - requirement_names()
    assert not unpinned, (
        "declared in setup.py's install_requires, absent from requirements.txt "
        f"(a deploy would not get them): {sorted(unpinned)}"
    )


# --- the `cli` extra stays out of the runtime set ---------------------------


def test_the_cli_extra_is_opt_in_and_not_a_runtime_dependency():
    """Decision F2: `keyring` never ships to the API or the worker. It belongs
    to the extra, so it is in neither the runtime set nor `requirements.txt`."""
    assert "keyring" in extra_names(CLI_EXTRA)
    assert "keyring" not in install_requires_names()
    assert "keyring" not in requirement_names()


@pytest.mark.parametrize(
    "package",
    ["alembic", "httpx2", "python-dateutil", "tenacity", "anthropic"]
    + ["google-api-python-client", "google-auth", "google-auth-oauthlib"],
)
def test_a_package_nothing_imports_stays_out_of_both_files(package):
    """The eight removed by #1216. Drive and the Meta Graph API are reached
    over httpx through the egress floor, never a Google client library, and
    nothing calls the Anthropic SDK; `alembic` is not the migration tool here
    (`scripts/migration_runner.py` is). Compared as PEP 503 names, so
    `google_auth` and `Google-Auth` are the same absence."""
    name = normalize(package)
    assert name not in requirement_names(), f"{package} is back in requirements.txt"
    assert name not in install_requires_names(), f"{package} is back in setup.py"
