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
untruth.** Inside a function declaring a chat-shaped parameter it matches four
shapes, all of which express the same hazard:

1. ``chat = settings.ADMIN_TELEGRAM_CHAT_ID`` — assignment to the parameter
2. ``x = chat or settings.ADMIN_TELEGRAM_CHAT_ID`` — the ``or`` form
3. ``x = chat if chat is not None else settings.ADMIN_TELEGRAM_CHAT_ID`` — the
   ternary
4. ``def f(chat=settings.ADMIN_TELEGRAM_CHAT_ID)`` — a parameter default

**Shapes 3 and 4 were added after review, and that is the point of this file
rather than a footnote to it.** The first version matched only 1 and 2 — and
shape 3 is exactly the idiom ``cli/commands/backfill.py`` uses, so the gate
written to stop this class recurring had a blind spot shaped precisely like the
code shipped beside it. The next person to write one would most plausibly copy
that idiom and sail straight past. A prevention mechanism with an undisclosed
hole in its own matching is the failure it exists to prevent, one layer up.

It does NOT flag an operator edge naming the admin chat where no chat parameter
is in scope — that is the target state (``cli/commands/queue.py`` and
``cli/commands/instagram.py``). Nor does it see a fallback routed through an
indirection it cannot resolve statically; it is a floor, not a proof.

## Sanctioned grants are DECLARED, not inferred

Widening to shape 3 makes the gate match the legitimate operator edge in
``backfill_instagram`` as well, and it should: structurally that line **is** the
hazard, and what makes it safe is context no parser can see. So the context is
written down. A site opts out with ``# admin-grant-ok:`` plus a reason on the
same line, which makes ``grep -rn "admin-grant-ok:"`` the standing inventory of
every deliberate admin grant in the tree — the move F.1 made with
``SYSTEM_SCOPE``, for the same reason: an exception that is explicit and
greppable is reviewable, and one the matcher quietly skips is not.

The allowlist is asserted by **equality** rather than merely tolerated, so a
second sanctioned grant is a visible line in a diff instead of headroom. That
rule is borrowed from the F.6 ratchet.
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


ALLOW_MARKER = "admin-grant-ok:"


def _allowlisted_lines(src: str) -> set:
    """Line numbers carrying an explicit sanctioned-grant marker."""
    return {
        i for i, line in enumerate(src.splitlines(), start=1) if ALLOW_MARKER in line
    }


def _names(node) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _is_fallback_expr(value, chat: set) -> bool:
    """`chat or ADMIN` (shape 2) or `chat if ... else ADMIN` (shape 3)."""
    if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
        return any(isinstance(v, ast.Name) and v.id in chat for v in value.values)
    if isinstance(value, ast.IfExp):
        # The parameter may appear in the test or in either branch, depending
        # on how the condition is phrased.
        seen = _names(value.test) | _names(value.body) | _names(value.orelse)
        return bool(seen & chat)
    return False


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
            src = path.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(src)
            except SyntaxError:  # pragma: no cover - not our concern here
                continue
            allowed = _allowlisted_lines(src)
            for fn in [
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]:
                chat = _chat_params(fn)
                if not chat:
                    continue
                rel = path.relative_to(root)

                # Shape 4: the fallback IS the parameter's default.
                defaults = list(fn.args.defaults) + [
                    d for d in fn.args.kw_defaults if d is not None
                ]
                for default in defaults:
                    if _mentions_admin(default) and default.lineno not in allowed:
                        found.append(f"{rel}:{default.lineno}::{fn.name}")

                # Shapes 1-3: an assignment in the body.
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                        continue
                    if not _mentions_admin(node.value):
                        continue
                    target = node.targets[0]
                    assigns_param = isinstance(target, ast.Name) and target.id in chat
                    if (
                        assigns_param or _is_fallback_expr(node.value, chat)
                    ) and node.lineno not in allowed:
                        found.append(f"{rel}:{node.lineno}::{fn.name}")
    return sorted(set(found))


def declared_admin_grants(root: pathlib.Path) -> list:
    """Every site that opted out, so the exception is an inventory."""
    out = []
    for top in ROOTS:
        base = root / top
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            src = path.read_text(encoding="utf-8", errors="ignore")
            for i, line in enumerate(src.splitlines(), start=1):
                if ALLOW_MARKER in line:
                    out.append(f"{path.relative_to(root)}:{i}")
    return sorted(out)


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

    def test_it_catches_the_TERNARY_form(self, tmp_path):
        """Review probe 1, verbatim. This shape returned [] from the first
        version of the gate — and it is the idiom this PR's own CLI fix uses,
        so the gate's blind spot was shaped exactly like the code beside it."""
        root = self._mk(
            tmp_path,
            "def f(telegram_chat_id=None):\n"
            "    resolved = (\n"
            "        telegram_chat_id\n"
            "        if telegram_chat_id is not None\n"
            "        else settings.ADMIN_TELEGRAM_CHAT_ID\n"
            "    )\n",
        )
        assert admin_fallback_sites(root) == ["src/m.py:2::f"]

    def test_it_catches_the_ternary_written_the_other_way_round(self, tmp_path):
        """The admin value in `body` rather than `orelse`. Same hazard, and a
        matcher that only looked at one branch would be a narrower fix wearing
        the same name."""
        root = self._mk(
            tmp_path,
            "def f(telegram_chat_id=None):\n"
            "    resolved = (\n"
            "        settings.ADMIN_TELEGRAM_CHAT_ID\n"
            "        if telegram_chat_id is None\n"
            "        else telegram_chat_id\n"
            "    )\n",
        )
        assert admin_fallback_sites(root) == ["src/m.py:2::f"]

    def test_it_catches_the_PARAMETER_DEFAULT_form(self, tmp_path):
        """Review probe 2, verbatim."""
        root = self._mk(
            tmp_path,
            "def f(telegram_chat_id: int = settings.ADMIN_TELEGRAM_CHAT_ID):\n"
            "    return telegram_chat_id\n",
        )
        assert admin_fallback_sites(root) == ["src/m.py:1::f"]

    def test_it_catches_a_keyword_only_parameter_default(self, tmp_path):
        """`kw_defaults` is a separate list on ast.arguments; reading only
        `defaults` would miss every keyword-only parameter."""
        root = self._mk(
            tmp_path,
            "def f(*, telegram_chat_id=settings.ADMIN_TELEGRAM_CHAT_ID):\n"
            "    return telegram_chat_id\n",
        )
        assert admin_fallback_sites(root) == ["src/m.py:1::f"]

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


class TestSanctionedGrantsAreDeclaredRatherThanInferred:
    """The widened gate matches the legitimate operator edge too, because
    structurally it IS the hazard. What makes it safe is context a parser
    cannot see, so the context is declared instead of guessed at."""

    def _mk(self, tmp_path, src):
        p = tmp_path / "cli" / "c.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        return tmp_path

    def test_a_marker_suppresses_the_finding(self, tmp_path):
        root = self._mk(
            tmp_path,
            "def cmd(chat_id=None):\n"
            "    target = chat_id if chat_id is not None else "
            "settings.ADMIN_TELEGRAM_CHAT_ID  # admin-grant-ok: operator edge\n",
        )
        assert admin_fallback_sites(root) == []
        assert declared_admin_grants(root) == ["cli/c.py:2"]

    def test_without_the_marker_the_same_line_IS_flagged(self, tmp_path):
        """Paired negative — otherwise the suppression test proves only that
        the gate found nothing, which it would also do if it were broken."""
        root = self._mk(
            tmp_path,
            "def cmd(chat_id=None):\n"
            "    target = chat_id if chat_id is not None else "
            "settings.ADMIN_TELEGRAM_CHAT_ID\n",
        )
        assert admin_fallback_sites(root) == ["cli/c.py:2::cmd"]

    def test_a_marker_on_the_wrong_line_does_not_suppress(self, tmp_path):
        """It is line-scoped on purpose: a marker floating elsewhere in the
        file would silence a site nobody meant to sanction."""
        root = self._mk(
            tmp_path,
            "# admin-grant-ok: not attached to anything\n"
            "def cmd(chat_id=None):\n"
            "    target = chat_id if chat_id is not None else "
            "settings.ADMIN_TELEGRAM_CHAT_ID\n",
        )
        assert admin_fallback_sites(root) == ["cli/c.py:3::cmd"]

    def test_the_real_trees_declared_grants_are_exactly_one(self):
        """Equality, not a ceiling — borrowed from the F.6 ratchet. A second
        sanctioned admin grant must arrive as a visible line in a diff."""
        assert declared_admin_grants(_repo()) == [
            "cli/commands/backfill.py:118",
        ], (
            "the set of DECLARED admin grants changed. Each one is a place "
            "where privilege is handed out on absent input on purpose; adding "
            "or moving one is a review decision, not a refactor."
        )
