"""The interaction-layer port.

Every inbound channel normalizes what it received into a :class:`Command` and
hands it to :func:`ingest`. The web/API adapter (`api/routes/v1.py`) is the
one that does so end to end; the Telegram webhook
(`/webhooks/telegram` — the `/start` door and the group join path) builds
a :class:`Command` in `telegram_dispatch`, while chat-inbound COMMANDS ("approve"
typed in a group) are not dispatched. Nothing inland sees a chat
id, a session cookie, a callback
payload or an HTTP body: a command carries RESOLVED domain ids and nothing
else, which is the discipline made structural rather than reviewed for.

## The vocabulary is the doc's, and the test proves it in both directions

`tests/fixtures/design_rows/01-target-architecture.md` prints the closed list;
it is the list's normative home, and adding a command is a deliberate change
there, ratchet-visible. :data:`VOCABULARY` is a COPY of that list, and
``tests/src/services/target/test_commands.py`` parses the doc and asserts
set-equality. So the enum cannot grow, shrink or drift without the doc moving
too — in either direction. A route table would have made the routes the
vocabulary; here the route (`POST …/commands/{command}`) merely validates
its path segment against this tuple.

## The order every adapter takes, written once

:func:`ingest` is refuse-cold → admit → execute, in the caller's ONE
transaction. `webhook_ingress` states the rule — *a delivery that cannot be
executed is refused BEFORE admission, never after*. So that no adapter
retypes it, the ordering lives here: an unknown name is
refused before any dedup row exists, admission (`command_dedup`, keyed by
the adapter's idempotency reference) precedes execution, and because all of
it shares the adapter's transaction a refusal anywhere rolls the dedup row
back and a retry re-executes. What stays with the adapter is only the shape
of its key — a web ``Idempotency-Key``, a Telegram ``update_id`` — and its
transport answers ("acknowledge fast" is not this module's).

## Not-built is a named refusal, not an absent name

A vocabulary command without an executor yet answers :class:`CommandNotBuilt`
(the adapter renders it 501, naming the command). It is NOT dropped from the
enum: the surface must advertise the contract it implements, and the set of
unbuilt names is pinned by the test so it can only shrink deliberately. The
gate runs BEFORE the not-built check, so a non-member cannot enumerate the
surface on a workspace they cannot see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

from src.exceptions.base import StorydumpError
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import tenant_resolution, vocabulary, webhook_ingress
from src.services.target.workspaces import InvalidWorkspaceArgs

#: The closed inbound vocabulary, in the doc's verbatim
#: order, owned by :mod:`src.services.target.vocabulary` (the one home the
#: CLI package may import) and re-exported here for every existing caller.
#: Set-equality with the doc is asserted by the unit gate.
VOCABULARY: tuple[str, ...] = vocabulary.COMMANDS

#: The floor ladder. ``user`` = any active signed-in user, no membership
#: (the workspace does not exist yet); ``member``/``admin``/``owner`` = the
#: `workspace_members` ladder `tenant_resolution.ROLE_ORDER` enforces;
#: ``operator`` = the product owner acting on the admin surface, and NOTHING
#: in this tier can currently produce that principal — see below.
FLOORS: tuple[str, ...] = ("user", "member", "admin", "owner", "operator")

# ``operator`` is the one floor with no principal behind it.
#
# No token reaches it: a person-bound token acts as its person, never above
# that person's membership role, and a workspace service identity never writes
# (`principal.refuse_token_write`). The `operator` AUDIT actor is a separate
# fact: `Principal.actor_kind` gives it to a service identity, `open_tenant`
# applies it, and `ck_audit_actor` permits it.
#
# Three coordinated places keep the floor unreachable: `Principal` carries no
# floor (a token's `token_role` decides only whether it may write), `Command`
# has no field that can express a non-user principal, and the refusal is TWO
# independent constructions (the branch in `authorize` below AND `operator`'s
# absence from `tenant_resolution.ROLE_ORDER`), so removing either alone changes
# nothing.
#
# `resolve_review` is not on the operator floor — see `ROLE_FLOOR`.
# `clear_quarantine` is, so no principal in this tier can run it.

#: Per-command floor.
ROLE_FLOOR: dict[str, str] = {
    "approve": "member",
    "skip": "member",
    "reject": "member",
    "mark_posted": "member",
    "cancel": "member",
    # A planned story: the floor of `approve` and `cancel`, the
    # two levers a member already holds on the same row. Scheduling asks a
    # person at the story's time and posts nothing by itself.
    "schedule_item": "member",
    "reschedule_item": "member",
    # The item's link: the same floor. It changes what a
    # story's card asks a person to add by hand, and posts nothing.
    "set_item_link": "member",
    # The item's Auto-approve flag: any member, because the flag
    # is a standing Approve, the lever a member already holds on each story.
    # It applies from the item's next serve and decides no card already
    # waiting.
    "set_item_auto_approve": "member",
    "autopost_now": "member",
    "sync_now": "member",
    "settings_change": "admin",
    # A folder's approval position: admin, like every
    # position — the workspace's own rides `settings_change`. YOLO on a
    # folder posts its stories without a tap.
    "set_source_approval": "admin",
    # Starts EQUAL to the workspace floor, and the equality is the point rather
    # than an oversight. What a separate kind buys is that the floors CAN
    # diverge — the gate below reads `ROLE_FLOOR[command.kind]`, keyed on kind
    # with no scope parameter, so a scope-carrying `settings_change` could only
    # ever apply one floor to workspace and account both. Starting them equal is
    # behaviourally identical to that and keeps the option open. Loosening this
    # later is cheap; tightening it is a security change, so it does not start
    # loose.
    "account_settings_change": "admin",
    "pause_workspace": "admin",
    "resume_workspace": "admin",
    "connect_account": "admin",
    "reconnect_account": "admin",
    "disconnect_account": "admin",
    "move_account": "admin",
    "disable_account": "admin",
    "create_workspace": "user",
    "rename_workspace": "admin",
    "offboard_workspace": "owner",
    "restore_workspace": "owner",
    "invite_member": "admin",
    # The floor of the command that made it: whoever may invite may withdraw
    # an invitation before it is accepted.
    "revoke_invitation": "admin",
    "remove_member": "admin",
    "change_role": "admin",
    "transfer_ownership": "owner",
    # First principles for many tenants: the `review_required` card is the
    # WORKSPACE's to resolve — a member holds `approve`, `mark_posted` and
    # `cancel` already, and the resolutions are those levers on a parked row
    # (`retry` re-approves, `posted` confirms, `cancel` gives up keeping the
    # debit). `failed` — a refund — is not offered: like `clear_quarantine`,
    # it is the operator's.
    "resolve_review": "member",
    "clear_quarantine": "operator",
}

#: The command each OAuth connect purpose stands for. The connect trio is
#: provider-general: a Drive leg and an Instagram leg read the same
#: entry, an issue leg reads the entry for the purpose it mints, and a callback
#: the entry for the purpose its state row carries, so every door to an intent
#: follows one floor and a callback cannot disagree with its issue leg.
CONNECT_PURPOSE_KIND: dict[str, str] = {
    "connect": "connect_account",
    "reconnect": "reconnect_account",
}


def connect_floor(purpose: str) -> str:
    """The floor of the command an OAuth connect leg for *purpose* stands for."""
    return ROLE_FLOOR[CONNECT_PURPOSE_KIND[purpose]]


def lowest_connect_floor() -> str:
    """The lower of the connect purposes' floors, where an issue leg that
    learns its purpose inside the session gates before reading it: no one
    either purpose admits is refused before the read, and nothing is read for
    anyone neither admits."""
    return min(
        (connect_floor(purpose) for purpose in CONNECT_PURPOSE_KIND),
        key=FLOORS.index,
    )


#: `CommandRefused.reason`, closed, owned by the vocabulary module. Adapters
#: map it without parsing prose, and the web adapter's status table is pinned
#: TOTAL over this tuple.
REASONS: tuple[str, ...] = vocabulary.REASONS


@dataclass(frozen=True)
class Command:
    """One normalized inbound command. Resolved ids only — never a chat id,
    a session value or a transport payload."""

    kind: str
    workspace_id: Optional[str]
    actor_user_id: str
    channel: str
    args: Mapping[str, Any] = field(default_factory=dict)
    #: The actor's display name as the ADAPTER resolved it (the tap reads it
    #: with the tapper's identity), never from `args`: the web route
    #: passes the request body into `args` verbatim, and a name there would
    #: be a member's own choice of what the group reads after "by".
    actor_label: Optional[str] = None
    #: The card the command came from, when the adapter knows it: the
    #: binding it was tapped in and the message ref Telegram named. A settled
    #: story's answer uses them to edit that message: to ADOPT a card with
    #: buttons the ledger never learned, or to edit again a known card whose
    #: newest edit did not land; a web command carries neither.
    binding_id: Optional[str] = None
    card_ref: Optional[str] = None


class AfterCommit(Protocol):
    """Work a command hands its adapter for after the commit: a provider call
    that cannot share the transaction, with an answer the person waits on. It
    receives the adapter's email sender and engine, and answers the keys of
    ``data`` it replaces. Implemented by `invitations.InvitationEmail`."""

    async def run(self, sender: Any, engine: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CommandResult:
    """``outcome`` is ``executed`` (an inline state flip happened) or
    ``enqueued`` (a job row now carries the work); ``data`` is what the
    adapter renders; ``after_commit``, never rendered, is :class:`AfterCommit`
    work the web adapter runs once the command's transaction has committed."""

    outcome: str
    data: dict
    after_commit: Optional[AfterCommit] = field(default=None, repr=False, compare=False)


class CommandRefused(StorydumpError):
    """A command the port will not run, with a reason from :data:`REASONS`.

    *facts* are what a front end needs to act on the refusal without parsing
    its prose — `locked` names what is in the way and whether an override
    would get past it. They ride the refusal's body under ``facts``, beside
    ``reason``."""

    def __init__(
        self,
        reason: str,
        detail: str = "",
        *,
        facts: Optional[Mapping[str, Any]] = None,
    ):
        if reason not in REASONS:
            raise ValueError(f"not a refusal reason: {reason!r}")
        self.reason = reason
        self.facts: dict[str, Any] = dict(facts or {})
        super().__init__(
            f"command refused: {reason}" + (f" — {detail}" if detail else "")
        )


class UnknownCommand(CommandRefused):
    def __init__(self, kind: str):
        self.command = kind
        super().__init__("unknown_command", kind)


class CommandNotBuilt(CommandRefused):
    def __init__(self, kind: str):
        self.command = kind
        super().__init__("not_built", f"{kind} has no executor in the target tier yet")


Executor = Callable[[Any, Command], Awaitable[CommandResult]]


def _build_registry() -> dict[str, Optional[Executor]]:
    # Imported here rather than at module top so the vocabulary and the floor
    # table are importable by adapters and tests without pulling the executor
    # modules' SQL dependencies along.
    from src.services.target import command_executors as ex

    registry: dict[str, Optional[Executor]] = {name: None for name in VOCABULARY}
    registry.update(
        {
            "approve": ex.approve,
            "skip": ex.skip,
            "reject": ex.reject,
            "mark_posted": ex.mark_posted,
            "cancel": ex.cancel,
            # a planned story, created and moved in place;
            # `cancel` ends one, as it ends any story.
            "schedule_item": ex.schedule_item,
            "reschedule_item": ex.reschedule_item,
            # the link an item's stories ask for by hand.
            "set_item_link": ex.set_item_link,
            # the item's Auto-approve flag.
            "set_item_auto_approve": ex.set_item_auto_approve,
            # a folder's approval position.
            "set_source_approval": ex.set_source_approval,
            "sync_now": ex.sync_now,
            # The trio is provider-general and
            # THIN: the OAuth leg is the API route's, these initiate and record.
            "connect_account": ex.connect_account,
            "reconnect_account": ex.reconnect_account,
            "disconnect_account": ex.disconnect_account,
            # The active ↔ disabled edge (a user command, audited), the disabling
            # half — the web's Remove.
            "disable_account": ex.disable_account,
            # An admin removes membership explicitly — the revoke for
            # every join edge, the Telegram one included.
            "remove_member": ex.remove_member,
            "settings_change": ex.settings_change,
            "account_settings_change": ex.account_settings_change,
            "pause_workspace": ex.pause_workspace,
            "resume_workspace": ex.resume_workspace,
            "create_workspace": ex.create_workspace,
            "invite_member": ex.invite_member,
            "revoke_invitation": ex.revoke_invitation,
            "rename_workspace": ex.rename_workspace,
            # The offboarding entry edge. The DOOR that finishes the job
            # (`fn_offboard_finalize`) is in `04_functions.sql`; the job kind
            # is declared in `machinery.py`.
            "offboard_workspace": ex.offboard_workspace,
            "restore_workspace": ex.restore_workspace,
            "resolve_review": ex.resolve_review,
        }
    )
    return registry


#: kind → executor, or None when the executor is not built. Total over the
#: vocabulary by construction; the None set is pinned by the unit gate.
REGISTRY: dict[str, Optional[Executor]] = _build_registry()

#: The named not-built set, pinned so that it shrinks only deliberately.
UNBUILT: tuple[str, ...] = tuple(k for k in VOCABULARY if REGISTRY[k] is None)


async def execute(session, command: Command) -> CommandResult:
    """Gate, then execute. The ONE path every adapter takes.

    *session* is the caller's open unit of work (tenant + actor GUCs already
    applied — the writer-identity rule, enforced by the audit triggers),
    bound to `command.workspace_id` for any command the gate checks
    (`tenant_resolution.authorize_member`).

    Order is load-bearing: unknown → refused cold (no gate, nothing to
    authorize against); then the gate; then not-built; then the executor. A
    refusal from the gate propagates as the `TenantResolutionError` it is —
    the adapter already maps that type (not-a-member → 404, below-floor → 403).
    A caller-supplied value the boundary refuses (`InvalidWorkspaceArgs`, from
    the writers or from a database CHECK) is one refusal, mapped here once.
    """
    if command.kind not in ROLE_FLOOR:
        raise UnknownCommand(command.kind)
    await authorize(
        session,
        command.kind,
        workspace_id=command.workspace_id,
        actor_user_id=command.actor_user_id,
    )

    executor = REGISTRY.get(command.kind)
    if executor is None:
        raise CommandNotBuilt(command.kind)
    try:
        return await executor(session, command)
    except InvalidWorkspaceArgs as exc:
        raise CommandRefused("invalid_args", str(exc)) from exc


async def authorize(
    session, kind: str, *, workspace_id: Optional[str], actor_user_id
) -> None:
    """The gate *kind*'s floor (`ROLE_FLOOR`) demands, or a refusal: the one
    spelling, for `execute` and for the Telegram card menu's navigation taps,
    which act on a card without running a command and must not be a
    lever a non-member can pull."""
    floor = ROLE_FLOOR[kind]
    if floor == "operator":
        # No principal this tier can build satisfies an operator floor (see
        # FLOORS). Refused as a ROLE refusal — deliberately not
        # `CommandNotBuilt` — so the surface never confirms to a member that an
        # operator command exists.
        raise TenantResolutionError(
            "insufficient_role", f"{kind} requires an operator principal"
        )
    if floor != "user":
        if not workspace_id:
            raise CommandRefused("workspace_required", kind)
        await tenant_resolution.authorize_member(
            session, workspace_id, actor_user_id, floor
        )


async def ingest(
    session,
    command: Command,
    *,
    external_ref: str,
    principal: str,
    payload: Any,
) -> CommandResult:
    """Refuse cold → admit → execute, in the caller's one transaction.

    *external_ref* and *principal* are the adapter's idempotency key
    (`command_dedup`'s ``(channel, principal, external_ref)``); *payload* is
    what the fingerprint is taken over — the adapter's raw body, so a replay
    of the same request matches regardless of what the adapter added to
    ``command.args``. Admission's own refusals (`DeliveryReplayed`,
    `AdmissionConflict`) propagate for the adapter to answer.
    """
    if command.kind not in ROLE_FLOOR:
        raise UnknownCommand(command.kind)
    await webhook_ingress.admit(
        session,
        channel=command.channel,
        external_ref=external_ref,
        payload=payload,
        principal=principal,
    )
    return await execute(session, command)
