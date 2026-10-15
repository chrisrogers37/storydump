"""The TARGET schema's declarative models — `TargetBase` (#746).

One declarative base (#1216). ``TargetBase`` carries the models for the schema
the target migration files create into the ``public`` that 051 leaves behind —
for schema parity, not for an ORM: the target tier's SQL is written by hand
under the unit of work.

**Why a base of its own rather than a list of target tables.** The lane parity
check compares ``create_all`` output against the replayed ``public`` schema, so
it needs to know which tables the models describe. A declared table list would
answer that with a second enumeration, maintained by hand, correct on the day
it is written and silently wrong at some later point nobody can predict.
``create_all`` on a base whose only members *are* the target models derives the
same answer from the models themselves: there is no list, so there is nothing
to drift. The check that requires no invented input beats the check that
requires a correct one.

**Lane parity is LOAD-BEARING from migration 053 on.** Migration 053 creates
seven tables and this module registers their models, so the comparison runs on
two populated schemas. That coupling is not a convention to remember — the
gate enforces it, because tables on only one side is exactly the drift it
reports.

**Lane parity compares `create_all` with the replayed `public` schema**, so
a `public` table a migration creates and this package does not model turns
that gate red — a "red and known" cost (#806).

**Importing a model module is what registers it**, so every model module is
imported here rather than only where it is used. Nothing below is decoration:
drop an import and its table silently leaves ``create_all``'s output, which
reads as a parity failure against the migration that installed it.
"""

from src.models.target.accounts_sources_media import (
    IgAccount,
    MediaItem,
    MediaSource,
    OAuthCredential,
    PostLock,
    ProviderQuarantine,
)
from src.models.target.auth_plane import (
    OAuthConnectionResult,
    OAuthState,
    ServiceToken,
    SessionToken,
)
from src.models.target.base import TargetBase
from src.models.target.machinery import (
    ChannelOutbox,
    CommandDedup,
    Job,
    ProviderOperation,
    RateCounter,
)
from src.models.target.intent_ledger import (
    AuditEvent,
    CategoryPostCaseMix,
    DailyPostCount,
    PostIntent,
    PostIntentTransition,
)
from src.models.target.identity_and_tenancy import (
    ChannelBinding,
    OnboardingSession,
    SignupAdmission,
    User,
    UserIdentity,
    WaitlistEntry,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
    WorkspaceMemberRemoval,
)

from src.models.target.meta_receipts import (
    MetaDeletionReceipt,
    MetaDeletionReceiptCapability,
    MetaDeletionReceiptGeneration,
    MetaDeletionReceiptScope,
    MetaIdentityMapping,
)
from src.models.target.instagram_catalog import (
    MediaSourceDestination,
    InstagramArchiveGeneration,
    InstagramSourcePolicy,
    InstagramMediaIndex,
    InstagramMediaAsset,
    InstagramItemTombstone,
    InstagramArtifact,
    InstagramIntentProvenance,
    InstagramOperationProvenance,
)

__all__ = [
    "MediaSourceDestination",
    "InstagramArchiveGeneration",
    "InstagramSourcePolicy",
    "InstagramMediaIndex",
    "InstagramMediaAsset",
    "InstagramItemTombstone",
    "InstagramArtifact",
    "InstagramIntentProvenance",
    "InstagramOperationProvenance",
    "MetaDeletionReceipt",
    "MetaDeletionReceiptCapability",
    "MetaDeletionReceiptGeneration",
    "MetaDeletionReceiptScope",
    "MetaIdentityMapping",
    "TargetBase",
    "AuditEvent",
    "CategoryPostCaseMix",
    "ChannelBinding",
    "ChannelOutbox",
    "CommandDedup",
    "DailyPostCount",
    "IgAccount",
    "Job",
    "MediaItem",
    "MediaSource",
    "OAuthConnectionResult",
    "OAuthCredential",
    "OAuthState",
    "OnboardingSession",
    "PostIntent",
    "PostIntentTransition",
    "PostLock",
    "ProviderOperation",
    "ProviderQuarantine",
    "RateCounter",
    "ServiceToken",
    "SessionToken",
    "SignupAdmission",
    "User",
    "UserIdentity",
    "WaitlistEntry",
    "Workspace",
    "WorkspaceInvitation",
    "WorkspaceMember",
    "WorkspaceMemberRemoval",
]
