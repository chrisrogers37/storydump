"""The §4 flip's tuple routing, at the unit (plan 03 D3, structural review of
#1306: "test_publish_cap.py units for the tuple").

The flip runs ONE CTE and decides on the row counts it reads back —
`(debited, flipped, was_approved, cancelling)`. The gate proves the CTE
against a real ledger (`tests/scripts/test_publish_cap_gate.py`); this file
pins the routing of every tuple the CTE can answer, on a scripted session,
so a re-ordered branch cannot turn a cancel into a deferral or a re-entry
into a raise without a red here.
"""

from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from src.services.target import publish_cap
from src.services.target.publish_cap import FlipOutcome, IntentNotApproved

SRC = Path(__file__).resolve().parents[4] / "src"

#: A write, not a mention. Matched in string literals only (`_sql_literals`):
#: docstrings, comments and the read views name the table too.
_BUCKET_WRITE = re.compile(
    r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+daily_post_counts", re.IGNORECASE
)


def _sql_literals(path: Path):
    """Every string literal in *path* but its docstrings, with its line.
    Adjacent literals are one node, so a statement split across lines reads
    whole; a comment is not in the tree at all."""
    tree = ast.parse(path.read_text())
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.lineno, node.value


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Result:
    def __init__(self, row):
        self._row = row

    def one(self):
        return self._row

    def fetchone(self):
        return self._row


class _Session:
    """A session that answers the flip's one statement with a scripted
    tuple, or raises what the driver would."""

    def __init__(self, *, row=None, raises=None):
        self._row = row
        self._raises = raises
        self.statements = []

    def begin_nested(self):
        return _Nested()

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        if self._raises is not None:
            raise self._raises
        return _Result(self._row)


def _tuple(debited, flipped, was_approved, cancelling):
    return SimpleNamespace(
        debited=debited,
        flipped=flipped,
        was_approved=was_approved,
        cancelling=cancelling,
    )


async def _flip(session):
    return await publish_cap.flip_to_publishing(
        session,
        intent_id="11111111-2222-3333-4444-555555555555",
        workspace_id="ws",
        ig_account_id="acct",
        local_date=date(2026, 9, 14),
        effective_cap=3,
    )


class TestTheFlipsTupleRouting:
    async def test_a_fresh_approval_debits_and_flips(self):
        assert await _flip(_Session(row=_tuple(1, 1, 1, False))) is FlipOutcome.PROCEED

    async def test_a_stepped_back_story_re_enters_without_a_debit(self):
        """(0,1,1): the debit it carries IS the debit (plan 03 D3)."""
        assert await _flip(_Session(row=_tuple(0, 1, 1, False))) is FlipOutcome.PROCEED

    async def test_the_day_at_its_cap_defers(self):
        assert await _flip(_Session(row=_tuple(0, 0, 1, False))) is FlipOutcome.DEFERRED

    async def test_a_cancel_flagged_row_is_the_cancel_route_never_a_deferral(self):
        assert await _flip(_Session(row=_tuple(0, 0, 1, True))) is FlipOutcome.CANCELLED

    async def test_no_approved_row_raises(self):
        with pytest.raises(IntentNotApproved):
            await _flip(_Session(row=_tuple(0, 0, 0, None)))

    async def test_a_debit_without_a_flip_raises_so_the_debit_rolls_back(self):
        with pytest.raises(IntentNotApproved):
            await _flip(_Session(row=_tuple(1, 0, 1, False)))

    async def test_the_exclusive_index_answers_busy(self):
        driver = SimpleNamespace(constraint_name=publish_cap._PUBLISH_EXCLUSIVE)
        exc = IntegrityError("INSERT", {}, driver)
        assert await _flip(_Session(raises=exc)) is FlipOutcome.BUSY

    async def test_any_other_integrity_error_propagates(self):
        driver = SimpleNamespace(constraint_name="ck_something_else")
        exc = IntegrityError("INSERT", {}, driver)
        with pytest.raises(IntegrityError):
            await _flip(_Session(raises=exc))

    async def test_the_statement_refuses_a_cancel_in_its_own_where(self):
        """The window between the pipeline's read and its flip is closed in
        SQL, not in Python: both the debit and the flip carry the guard."""
        session = _Session(row=_tuple(1, 1, 1, False))
        await _flip(session)
        sql = session.statements[0][0]
        assert sql.count("cancel_requested") >= 3, sql
        assert "AND NOT p.cancel_requested" in sql
        assert "me.cap_consumed_on IS NULL AND NOT me.cancel_requested" in sql


class TestOnlyTheCadenceSpendsTheCap:
    """Content schedule F8 (a): a planned story neither spends the day nor
    waits on a spent one. The gate proves each answer against a real ledger
    (`TestAPlannedStorySitsOutsideTheCap`); these two hold the structure that
    keeps the answers from drifting apart."""

    async def test_both_refund_legs_run_the_one_decrement(self):
        refund, retry = _Session(row=("id",)), _Session(row=("id",))
        await publish_cap.refund_cap(
            refund, intent_id="i", workspace_id="ws", ig_account_id="acct"
        )
        await publish_cap.resolve_retry(
            retry,
            intent_id="i",
            workspace_id="ws",
            ig_account_id="acct",
            attempts_by_step={"v": 1},
        )
        decrement = (
            publish_cap._RETURN_DEBIT,
            {"ws": "ws", "acct": "acct", "intent": "i"},
        )
        assert refund.statements[0] == decrement
        assert retry.statements[0] == decrement

    def test_only_the_cap_ledger_writes_a_bucket(self):
        """Every write to `daily_post_counts` asks one predicate, and it can
        only do that where the predicate is. The phase plan missed a second
        debit, Posted myself, because it lived in another module. A new SQL
        string literal under `src/` that writes the table by its bare name
        fails here until it moves into `publish_cap.py`; the scan does not see
        ORM or Core writes, a built or schema-qualified name, or a write
        outside `src/`."""
        writers = [
            f"{p.relative_to(SRC)}:{line}"
            for p in sorted(SRC.rglob("*.py"))
            for line, literal in _sql_literals(p)
            if _BUCKET_WRITE.search(literal)
        ]
        assert writers, f"no bucket writer found under {SRC}: the scan went stale"
        assert {w.rsplit(":", 1)[0] for w in writers} == {
            "services/target/publish_cap.py"
        }, writers
