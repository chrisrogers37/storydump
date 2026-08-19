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

**Lane parity is LOAD-BEARING from F.2.2 on.** It was vacuous while both sides
were empty and said so; migration 053 landed 02 §1's seven tables and this
module registers their models in the same increment, so the comparison now runs
on two populated schemas. That coupling is not a convention to remember — the
gate enforces it, because tables on only one side is exactly the drift it
reports.

**Models land per increment, not in one pass.** The alternative was considered
and the gate settled it rather than taste: deferring every model to one late
pass means running lane parity knowingly red from F.2.2 through F.2.7, which is
the "red and known" cost #806 Fork 1 declined when it declined option D.

**Importing a model module is what registers it**, so every increment's module
is imported here rather than only where it is used. Nothing below is
decoration: drop an import and its table silently leaves ``create_all``'s
output, which reads as a parity failure against the migration that installed it.

At M.3 the application is flipped over to this base — a visible switch, not a
rewrite.
"""

from src.models.target.accounts_sources_media import (
    IgAccount,
    MediaItem,
    MediaSource,
    OAuthCredential,
    PostLock,
    ProviderQuarantine,
)
from src.models.target.base import TargetBase
from src.models.target.identity_and_tenancy import (
    ChannelBinding,
    OnboardingSession,
    User,
    UserIdentity,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)

__all__ = [
    "TargetBase",
    "ChannelBinding",
    "IgAccount",
    "MediaItem",
    "MediaSource",
    "OAuthCredential",
    "OnboardingSession",
    "PostLock",
    "ProviderQuarantine",
    "User",
    "UserIdentity",
    "Workspace",
    "WorkspaceInvitation",
    "WorkspaceMember",
]
