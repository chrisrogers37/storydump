"""No route reads its request body while it holds a tenant transaction (#1644).

A body read inside an open unit of work holds a pooled connection for as long as
the client takes to send it. The pool is ten connections and the API's wait for
one is a second (`unit_of_work.INGRESS_POOL_TIMEOUT_SEAM`), so a few direct
clients that dribble a body can leave every other request refused its
connection. The web sends its body at once, which is why nothing shows it.

The rule is "read and parse, then open the session", as `put_category_mix` and
the waitlist route do. This pins it in the source, so a route that grows the
habit fails here, in either of two shapes:

* an ``async with`` over a session opener whose block awaits a body read;
* a function handed an open session (a parameter named ``session``) that reads
  the body itself.

It sees a read made in the block or the function itself, not one a helper makes
for it. What the routes DO is proven under a slow body in
`tests/scripts/test_body_before_session_gate.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[3] / "src" / "api" / "routes"

#: What opens a transaction a request then holds: the tenant seams of
#: `principal.py`, and an engine's own `begin()` / `connect()`.
SESSION_OPENERS = frozenset(
    {
        "open_tenant",
        "floor_session",
        "member_session",
        "admin_session",
        "connect_session",
        "reader_session",
        "begin",
        "connect",
    }
)
#: The `Request` methods that read the body.
BODY_METHODS = frozenset({"body", "json", "form", "stream"})


def _name_of(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _reads_a_body(node: ast.AST) -> bool:
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if _name_of(func) == "json_object":
            return True
        if (
            isinstance(func, ast.Attribute)
            and func.attr in BODY_METHODS
            and isinstance(func.value, ast.Name)
            and func.value.id == "request"
        ):
            return True
    return False


def _opens_a_session(item: ast.withitem) -> bool:
    call = item.context_expr
    return isinstance(call, ast.Call) and _name_of(call.func) in SESSION_OPENERS


def body_reads_in_a_session(root: Path) -> set[tuple[str, str, str]]:
    """``(file, function, what)`` for each read of a body inside a session."""
    found = set()
    for path in sorted(root.glob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {a.arg for a in (*fn.args.args, *fn.args.kwonlyargs)}
            if "session" in params and _reads_a_body(fn):
                found.add(
                    (path.name, fn.name, "is handed a session and reads the body")
                )
            for block in ast.walk(fn):
                if (
                    isinstance(block, ast.AsyncWith)
                    and any(_opens_a_session(item) for item in block.items)
                    and any(_reads_a_body(stmt) for stmt in block.body)
                ):
                    found.add((path.name, fn.name, "reads the body inside its session"))
    return found


def test_no_route_reads_its_body_inside_a_tenant_session():
    assert body_reads_in_a_session(ROUTES) == set()


def test_the_guard_sees_both_shapes_and_not_a_route_that_reads_first(tmp_path):
    (tmp_path / "rogue.py").write_text(
        "async def inside(request, principal):\n"
        "    async with principal_mod.admin_session(request, ws, principal) as s:\n"
        "        body = await principal_mod.json_object(request)\n"
        "\n"
        "async def raw(request, engine):\n"
        "    async with engine.begin() as conn:\n"
        "        raw = await request.body()\n"
        "\n"
        "async def handed(session, request):\n"
        "    return await request.json()\n"
        "\n"
        "async def first(request, principal):\n"
        "    body = await principal_mod.json_object(request)\n"
        "    async with principal_mod.admin_session(request, ws, principal) as s:\n"
        "        await write(s, body)\n"
        "\n"
        "async def unrelated(request):\n"
        "    async with client.stream('GET', url) as response:\n"
        "        return response.json()\n"
    )
    assert body_reads_in_a_session(tmp_path) == {
        ("rogue.py", "inside", "reads the body inside its session"),
        ("rogue.py", "raw", "reads the body inside its session"),
        ("rogue.py", "handed", "is handed a session and reads the body"),
    }
