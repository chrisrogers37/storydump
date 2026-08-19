"""The one tenant resolver: inbound identity -> tenant id (F.3, #842).

`04` F.3: *"one resolver: inbound (chat id | web session) -> workspace_id; all
service boundaries speak tenant_id == workspaces.id from here on."* This module
is that resolver. Everything below is about the two things the increment can
actually be held to today — being ONE, and being NEUTRAL — and about the one
part that is gated on M.3.

## How this relates to F.1's chokepoint — they compose, neither replaces

F.1 (#841) installed a fail-closed tenant chokepoint in
``src/repositories/tenant_scope.py``. It is easy to read the two increments as
overlapping. They do not, and the distinction is what keeps this from becoming
the second tenant path it exists to remove:

    inbound identity  --[ THIS MODULE ]-->  tenant id  --[ F.1 chokepoint ]-->  SQL
    (chat id | web)        resolves                        refuses absence

* F.1 is a **guard**: given a tenant id, ``require_tenant_context`` refuses
  None and demands ``SYSTEM_SCOPE`` for deliberate cross-tenant access. It
  resolves nothing.
* This is a **resolver**: it turns an inbound identity into that id. It
  guards nothing.

So the resolver does **not** call the chokepoint and must not: a resolver that
also enforced would give call sites two places to be refused from, and the
repository layer would stop being the single place tenant absence is caught.
It sits strictly upstream.

**One vocabulary, not two.** The resolver returns F.1's own ``TenantScope``
type rather than minting a parallel one. That is deliberate: a second tenant
type flowing through the same call graph is exactly the drift F.3 exists to
remove, and it would be invisible — both would be strings.

## What is M.3-gated, stated rather than quietly skipped

`04` says boundaries speak ``workspaces.id``. **They cannot yet, and this is
measured rather than assumed:** ``src/models/target`` has zero runtime
consumers (nothing outside that package imports ``TargetBase``), the running
application is bound to ``src.config.database.Base``, and there is no
``workspaces`` table in the legacy schema it reads. The tenant root today is
``chat_settings.id``.

So the value stays ``chat_settings.id`` and the **vocabulary** goes neutral
now. That is the useful half, and it is what makes M.3 cheap: at cutover the
key changes in the two lookup bodies below, not at every call site that names a
tenant. The resolver is the seam; ``workspaces.id`` is a one-place swap behind
it.

## Why the missing-tenant policy is a PARAMETER, not a decision made here

The five resolution sites this consolidates do **not** agree on what an unknown
chat means, and that was measured before any of this was written:

    media_lock ................. silent code default
    membership_service ......... False (deny)
    onboarding/dashboard route . HTTPException 404
    google_drive_oauth ......... ValueError
    dashboard_service .......... CREATES the tenant (get_or_create)

Any single built-in policy therefore changes behaviour at four sites out of
five. The last one matters most: resolution there is also a **provisioning**
path, so a resolver that quietly adopted it would let any unknown chat id
create a tenant, and one that dropped it would break first-contact bootstrap.

The policy is consequently an explicit, greppable argument — the same move F.1
made with ``SYSTEM_SCOPE``, for the same reason: a consequential default that
is inherited silently is the thing being retired. ``PROVISION`` is named to be
conspicuous in a diff, because it is the one that writes.

    grep -rn "PROVISION" src/    # the standing inventory of creating paths

## Neutrality is about the channel, and the web shape is not what it looks like

`04` names two inbound shapes. Measured, there is currently **one**:
``src/utils/webapp_auth.py`` validates a Mini-App ``init_data`` payload or a
signed URL token and returns a ``chat_id`` — so the "web session" path already
funnels into the Telegram chat id before any tenant lookup happens. Both entry
points are still exposed here, because the shapes are distinct to a *caller*
and will diverge at M.3 when a real web session resolves to a workspace
directly. What is not claimed is that they exercise two different lookups
today; they do not, and pretending otherwise would be a second path in waiting.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from src.exceptions.base import StorydumpError
from src.repositories.tenant_scope import TenantScope

#: Unknown tenant -> raise :class:`TenantResolutionError`.
REFUSE = "refuse"
#: Unknown tenant -> return a resolution whose ``tenant_id`` is None.
ABSENT = "absent"
#: Unknown tenant -> CREATE one. Writes. Named to be loud in a diff.
PROVISION = "provision"

_POLICIES = (REFUSE, ABSENT, PROVISION)


class TenantResolutionError(StorydumpError):
    """No tenant could be resolved for an inbound identity under ``REFUSE``."""


class TenantResolution(NamedTuple):
    """The resolved tenant, and how it was reached.

    ``created`` is carried rather than inferred: a caller that passed
    ``PROVISION`` usually wants to know whether this call was the one that
    brought the tenant into existence (first-contact bootstrap emits a
    different response than a returning chat), and re-deriving that with a
    second lookup would be a second resolution path.
    """

    tenant_id: Optional[str]
    created: bool
    origin: str

    @property
    def scope(self) -> TenantScope:
        """The value F.1's repository chokepoint expects.

        Not ``SYSTEM_SCOPE`` on absence — deliberately. Widening a query is
        precisely what F.1 retired, so an unresolved tenant yields None and the
        chokepoint refuses it downstream.
        """
        return self.tenant_id


def _check_policy(on_missing: str) -> None:
    if on_missing not in _POLICIES:
        raise ValueError(
            f"on_missing must be one of {_POLICIES}, got {on_missing!r} — the "
            "missing-tenant policy is explicit at the call site (F.3/#842)"
        )


def _lookup_chat(telegram_chat_id: int, on_missing: str, settings_repo):
    """THE tenant lookup. At M.3 this body resolves a workspace instead.

    Kept in one function on purpose: it is the whole reason the increment is
    called "one resolver". Every inbound shape funnels here, so the M.3 key
    change has exactly one site.
    """
    if on_missing == PROVISION:
        record = settings_repo.get_or_create(telegram_chat_id)
        # get_or_create cannot report which branch it took, so "created" is
        # not claimed rather than guessed. A caller needing that distinction
        # should look the tenant up with ABSENT first.
        return record, False
    return settings_repo.get_by_chat_id(telegram_chat_id), False


def resolve_tenant_from_chat_id(
    telegram_chat_id: int,
    *,
    on_missing: str = REFUSE,
    settings_repo=None,
) -> TenantResolution:
    """Resolve a Telegram chat id to its tenant id.

    ``settings_repo`` is injectable so adapter tests drive the real resolution
    logic without a database; production passes nothing and gets the repository.
    """
    _check_policy(on_missing)
    if telegram_chat_id is None:
        raise TenantResolutionError(
            "resolve_tenant_from_chat_id: no chat id supplied — an absent "
            "inbound identity is never a tenant (F.3/#842)"
        )

    if settings_repo is None:
        from src.repositories.chat_settings_repository import ChatSettingsRepository

        with ChatSettingsRepository() as repo:
            record, created = _lookup_chat(telegram_chat_id, on_missing, repo)
            tenant_id = str(record.id) if record is not None else None
    else:
        record, created = _lookup_chat(telegram_chat_id, on_missing, settings_repo)
        tenant_id = str(record.id) if record is not None else None

    if tenant_id is None and on_missing == REFUSE:
        raise TenantResolutionError(
            f"resolve_tenant_from_chat_id: no tenant for chat {telegram_chat_id}"
        )
    return TenantResolution(tenant_id, created, "telegram_chat")


def resolve_tenant_from_web_session(
    session: dict,
    *,
    on_missing: str = REFUSE,
    settings_repo=None,
) -> TenantResolution:
    """Resolve a validated web session to its tenant id.

    *session* is the dict ``src.utils.webapp_auth`` returns from
    ``validate_init_data`` / ``validate_url_token``. **This function does not
    validate it** — authentication is the adapter's job and doing it here would
    make the resolver a second auth surface.

    Today the session carries a ``chat_id``, so this delegates to the chat-id
    lookup rather than duplicating one. The ``origin`` it reports is still
    ``web_session``, because which surface a request arrived on is a fact
    callers and audits care about even while the lookup is shared.
    """
    _check_policy(on_missing)
    chat_id = (session or {}).get("chat_id")
    if chat_id is None:
        raise TenantResolutionError(
            "resolve_tenant_from_web_session: the validated session carries no "
            "chat_id, so no tenant can be resolved from it (F.3/#842)"
        )
    resolved = resolve_tenant_from_chat_id(
        chat_id, on_missing=on_missing, settings_repo=settings_repo
    )
    return TenantResolution(resolved.tenant_id, resolved.created, "web_session")
