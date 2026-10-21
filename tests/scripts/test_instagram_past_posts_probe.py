"""The feasibility CLI refuses every probe without reading or contacting anything."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import instagram_past_posts_probe as probe

REFUSAL = {"v": 1, "outcome": "refused", "refusal_code": "feasibility_probe_retired"}
SECRET = "private-token-caption-signed-url"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--workspace", SECRET, "--account", SECRET],
        ["--graph-version", SECRET, "--before", SECRET, "--unknown", SECRET],
    ],
)
def test_probe_is_deterministic_and_never_echoes_arguments(args, capsys):
    assert probe.main(args) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == REFUSAL
    assert SECRET not in captured.out and captured.err == ""


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_probe_remains_discoverable_without_claiming_live_flows(flag, capsys):
    assert probe.main([flag, "--account", SECRET]) == 0
    output = capsys.readouterr().out
    assert "--workspace" in output and "--before" in output
    assert "only where the past-posts source is activated" in output
    assert "always refuses" in output and SECRET not in output


def test_fresh_interpreter_refuses_with_all_sensitive_io_paths_prohibited():
    # Read the CLI source before installing guards; nothing in its execution
    # may access environment, files, credentials, DB libraries or networking.
    bootstrap = r"""
import builtins, os, socket, sys
source = open(sys.argv[1]).read()
path = sys.argv[1]

def forbidden(*args, **kwargs):
    raise AssertionError('retired probe attempted I/O')

class ForbiddenEnvironment:
    __getitem__ = get = __iter__ = __len__ = forbidden

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split('.')[0] in {'src', 'httpx', 'sqlalchemy', 'asyncpg', 'psycopg', 'requests', 'PIL'}:
        forbidden()
    return original_import(name, *args, **kwargs)

# Load only the CLI's stdlib JSON dependency before the execution guard.
import json
builtins.__import__ = guarded_import
builtins.open = os.open = socket.socket = os.getenv = forbidden
os.environ = ForbiddenEnvironment()
sys.argv = [path, '--workspace', 'private-workspace', '--account', 'private-token']
exec(compile(source, path, 'exec'), {'__name__': '__main__'})
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", bootstrap, str(Path(probe.__file__).resolve())],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 2 and result.stderr == ""
    assert json.loads(result.stdout) == REFUSAL
    assert "private" not in result.stdout
