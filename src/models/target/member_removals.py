"""Plan 07 §37 — the member-removal record (migration 094).

The mirror of `094_member_removal_record.sql`, on the terms `auth_plane` set:
the second side of the `04` §0.2 parity gate, not an independent design. The
table's RLS and its one policy (svc_membership's) have no declarative
SQLAlchemy representation, so nothing about them shows here.

**The key is the pair.** One row per (workspace, person) an admin has removed;
a later removal of the same person updates that row rather than adding one.
**The remover is SET NULL, the rest CASCADE:** deleting the remover's account
keeps the record, and deleting the workspace or the person takes it with them.
"""

from src.models.target.base import TargetBase
from src.models.target.columns import fk, timestamps


class WorkspaceMemberRemoval(TargetBase):
    """A person an admin removed from a workspace. The join door adds nobody a
    row names; a membership row outranks it, so an accepted invitation makes
    the person a member again."""

    __tablename__ = "workspace_member_removals"

    workspace_id = fk("workspaces.id", "CASCADE", primary_key=True)
    user_id = fk("users.id", "CASCADE", primary_key=True)
    removed_by_user_id = fk("users.id", "SET NULL", nullable=True)
    created_at, updated_at = timestamps()
