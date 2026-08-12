"""CI parity gate: the ``posting_history`` CHECK constraints and the migration
DDL must match the code-owned Enums (``src/models/enums.py``).

This is the mechanism that ends the ``check_posting_method`` drift class (#684,
#685): adding a value in code without the migration — or vice versa — fails
THIS test instead of aborting a production scheduler sweep with a
``CheckViolation``.

The migration-side checks carry the real teeth: the model constraint is now
*derived* from the Enum (via ``sql_in_list``), so the model-side checks only
guard against someone reverting to a hand-typed list — they cannot catch
enum-vs-applied-DDL drift. The migration is independently hand-authored SQL, so
the migration-side checks are what actually catch a value added to the Enum
without its migration.

M4 limitation (rajan, ironclad review of #692): the strongest form of this gate
would introspect the constraints on a migration-REPLAYED database, proving the
value actually APPLIED to prod matches the Enum. The current test harness
bootstraps the schema with SQLAlchemy ``Base.metadata.create_all``
(``tests/conftest.py::setup_test_database``), NOT by replaying
``scripts/migrations/*.sql`` — so any DB introspection here would only ever see
the MODEL constraint, never the applied migration. We therefore assert
statically that (1) the MODEL-declared constraint matches the Enum, and (2) the
latest migration FILE that adds the constraint matches the Enum. The
migration-replay harness this note used to defer to now exists:
``tests/scripts/test_migration_gate.py`` replays the corpus through the runner
and compares ``pg_constraint`` definitions on the replayed database against the
models (plan 0.2). This file keeps the cheap unit-tier text check; the replayed
introspection lives there.
"""

import re

import pytest

from src.models.enums import HistoryStatus, PostingMethod, QueueStatus
from src.models.posting_history import PostingHistory
from src.models.posting_queue import PostingQueue
from src.utils.validators import MIGRATIONS_DIR


def _quoted_values(text):
    """The set of single-quoted string literals in ``text``."""
    return set(re.findall(r"'([^']+)'", text))


def _model_constraint_values(model, constraint_name):
    """Quoted values in the model's named CHECK constraint (__table_args__)."""
    constraint = next(
        c for c in model.__table_args__ if getattr(c, "name", None) == constraint_name
    )
    return _quoted_values(str(constraint.sqltext))


def _latest_migration_in_list(constraint_name):
    """Quoted values in the highest-numbered migration that ADDs the constraint."""
    pattern = re.compile(
        rf"ADD CONSTRAINT {constraint_name}\s+CHECK\s*\(\s*\w+\s+IN\s*\(([^)]*)\)",
        re.IGNORECASE | re.DOTALL,
    )
    best_num, best_body = -1, None
    for path in MIGRATIONS_DIR.glob("*.sql"):
        version = re.match(r"^(\d+)_", path.name)
        num = int(version.group(1)) if version else -1
        match = pattern.search(path.read_text())
        if match and num > best_num:
            best_num, best_body = num, match.group(1)
    assert best_body is not None, f"no migration ADDs {constraint_name}"
    return _quoted_values(best_body)


@pytest.mark.unit
class TestEnumSSOTParity:
    def test_model_posting_method_matches_enum(self):
        assert _model_constraint_values(PostingHistory, "check_posting_method") == {
            m.value for m in PostingMethod
        }

    def test_model_history_status_matches_enum(self):
        assert _model_constraint_values(PostingHistory, "check_history_status") == {
            m.value for m in HistoryStatus
        }

    def test_latest_migration_posting_method_matches_enum(self):
        assert _latest_migration_in_list("check_posting_method") == {
            m.value for m in PostingMethod
        }

    def test_latest_migration_history_status_matches_enum(self):
        assert _latest_migration_in_list("check_history_status") == {
            m.value for m in HistoryStatus
        }

    def test_model_queue_status_matches_enum(self):
        assert _model_constraint_values(PostingQueue, "check_status") == {
            m.value for m in QueueStatus
        }

    def test_latest_migration_queue_status_matches_enum(self):
        assert _latest_migration_in_list("check_status") == {
            m.value for m in QueueStatus
        }
