"""The TARGET schema's declarative models — the second `Base` (#746, fork (a)).

Two declarative bases coexist until the M.3 cutover, deliberately:

- ``src.config.database.Base`` carries the LEGACY models. The running
  application — services, repositories, the CLI — is built on them and keeps
  running on them, untouched, until cutover. They describe the schema that
  lives in ``legacy`` from migration 051 on.
- ``TargetBase`` carries the models for the schema the F.2 migration files
  create into the empty ``public`` that 051 leaves behind.

**Why two bases rather than one base plus a list of target tables.** The lane
parity check compares ``create_all`` output against the replayed schema, and it
needs to know which tables belong to which lineage. A single base forces that
question to be answered by a declared target-table list — a second enumeration,
maintained by hand, correct on the day it is written and silently wrong at some
later point nobody can predict. ``create_all`` on a base whose only members
*are* the target models derives the same answer from the models themselves:
there is no list, so there is nothing to drift. The check that requires no
invented input beats the check that requires a correct one.

**It is empty right now, and that is a real state rather than a stub.** Zero
target tables exist today; F.2.2 onward registers them here. The lane parity
test asserts the emptiness explicitly rather than passing silently on it, so
the first model to land forces a deliberate update instead of quietly
converting a vacuous green into a load-bearing one.

At M.3 the application is flipped over to this base — a visible switch, not a
rewrite.
"""

from src.models.target.base import TargetBase

__all__ = ["TargetBase"]
