"""The database's declarative table definitions.

`TargetBase.metadata` is the table inventory that `ddl.build()` creates. Every
model module is imported here so its tables register before the builder runs.
The models define columns, defaults, constraints and indexes. The SQL files in
`src/models/target/ddl/` define roles, functions, reference rows, triggers,
row-level security, ownership and grants. Services issue SQL under a unit of
work; these models define the schema rather than ORM write behavior.
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
    InstagramPastPostsGeneration,
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
    "InstagramPastPostsGeneration",
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
