"""#867 — no function may grant the ADMIN tenant on absent input.

F.1 made the fail-open ``if chat_settings_id:`` pattern extinct at the
repository layer. An implicit ``ADMIN_TELEGRAM_CHAT_ID`` default when a chat is
absent is that same shape wearing a default: it **grants privilege on missing
input**, and it does so silently, which is worse than the guard F.1 retired
because there is no branch to notice.

This is a structural gate rather than a set of per-function tests, for the
reason the F.6 ratchet exists: the hazard is a *shape*, and a shape needs a
predicate that catches the next instance, not only today's eight. A new
function written next week with the same default would pass every behavioural
test in the suite and be caught only here.

**Scope, stated because a gate that reads broader than it is becomes its own
untruth.** It matches an assignment, inside a function that declares a
chat-shaped parameter, whose value is or falls back to
``settings.ADMIN_TELEGRAM_CHAT_ID``. It deliberately does NOT flag an operator
edge naming the admin chat at its own call site — that is the target state, not
the defect (``cli/commands/queue.py`` and ``cli/commands/instagram.py`` do
exactly this and must keep passing). Nor does it see a fallback routed through
an indirection it cannot resolve statically; it is a floor, not a proof.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOTS = ("src", "cli")
ADMIN = "ADMIN_TELEGRAM_CHAT_ID"


def _repo() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _mentions_admin(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == ADMIN for n in ast.walk(node))


def _chat_params(fn) -> set:
    args = fn.args
    names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
    return {n for n in names if "chat" in n.lower() or "tenant" in n.lower()}


def admin_fallback_sites(root: pathlib.Path) -> list:
    """Every implicit absent-chat -> ADMIN grant, as `path:line::function`."""
    found = []
    for top in ROOTS:
        base = root / top
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:  # pragma: no cover - not our concern here
                continue
            for fn in [
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]:
                chat = _chat_params(fn)
                if not chat:
                    continue
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                        continue
                    target = node.targets[0]
                    if not _mentions_admin(node.value):
                        continue
                    # `x = ADMIN` where x is the chat param itself, or
                    # `y = x or ADMIN` where x is the chat param.
                    assigns_param = isinstance(target, ast.Name) and target.id in chat
                    ors_param = (
                        isinstance(node.value, ast.BoolOp)
                        and isinstance(node.value.op, ast.Or)
                        and any(
                            isinstance(v, ast.Name) and v.id in chat
                            for v in node.value.values
                        )
                    )
                    if assigns_param or ors_param:
                        rel = path.relative_to(root)
                        found.append(f"{rel}:{node.lineno}::{fn.name}")
    return sorted(set(found))


def test_no_function_grants_the_admin_tenant_on_absent_input():
    """The gate. #867 retired all 8 sites that existed when it was written."""
    sites = admin_fallback_sites(_repo())
    assert sites == [], (
        "an implicit absent-chat -> ADMIN grant was reintroduced. This is the "
        "F.1 fail-open shape wearing a default: it grants privilege on missing "
        "input, silently. Make the chat parameter REQUIRED and have the "
        "operator edge (a CLI command, an admin path) name "
        "settings.ADMIN_TELEGRAM_CHAT_ID at its own call site.\n  " + "\n  ".join(sites)
    )


class TestTheGateCanActuallySee:
    """A gate asserting an empty set passes vacuously against any tree it
    cannot read. These are its positive controls."""

    def _mk(self, tmp_path, src):
        p = tmp_path / "src" / "m.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        return tmp_path

    def test_it_catches_the_if_none_form(self, tmp_path):
        root = self._mk(
            tmp_path,
            "def f(telegram_chat_id=None):\n"
            "    if telegram_chat_id is None:\n"
            "        telegram_chat_id = settings.ADMIN_TELEGRAM_CHAT_ID\n",
        )
        assert admin_fallback_sites(root) == ["src/m.py:3::f"]

    def test_it_catches_the_or_form(self, tmp_path):
        root = self._mk(
            tmp_path,
            "def f(telegram_chat_id=None):\n"
            "    chat_id = telegram_chat_id or settings.ADMIN_TELEGRAM_CHAT_ID\n",
        )
        assert admin_fallback_sites(root) == ["src/m.py:2::f"]

    def test_an_operator_edge_naming_the_admin_chat_is_NOT_flagged(self, tmp_path):
        """The target state must not trip the gate, or the fix is unshippable."""
        root = self._mk(
            tmp_path,
            "def cmd():\n"
            "    return svc.count_pending(settings.ADMIN_TELEGRAM_CHAT_ID)\n",
        )
        assert admin_fallback_sites(root) == []

    def test_a_required_parameter_is_NOT_flagged(self, tmp_path):
        root = self._mk(
            tmp_path,
            "def f(telegram_chat_id):\n    return use(telegram_chat_id)\n",
        )
        assert admin_fallback_sites(root) == []


@pytest.mark.parametrize("top", ROOTS)
def test_the_gate_actually_reached_the_tree(top):
    """Second positive control, on the REAL tree: an empty finding above must
    mean 'scanned and clean', never 'scanned nothing'."""
    files = list((_repo() / top).rglob("*.py"))
    assert len(files) > 5, f"{top}/ looks unscanned — the gate would read clean"
