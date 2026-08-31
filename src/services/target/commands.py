"""The interaction-layer port (`01` §Interaction-layer port, FC-2; task #1028).

Every inbound channel — the web/API adapter today, the Telegram webhook (W4)
next — normalizes what it received into a :class:`Command` and hands it to
:func:`ingest`. Nothing inland sees a chat id, a session cookie, a callback
payload or an HTTP body: a command carries RESOLVED domain ids and nothing
else, which is the FC-2 discipline made structural rather than reviewed for.

## The vocabulary is the doc's, and the test proves it in both directions

`01-target-architecture.md` §Interaction-layer port prints the closed list and
says it "is its normative home, and adding a command is a deliberate change
here, ratchet-visible." :data:`VOCABULARY` is a COPY of that list, and
``tests/src/services/target/test_commands.py`` parses the doc and asserts
set-equality. So the enum cannot grow, shrink or drift without the doc moving
too — in either direction. A route table would have made the routes the
vocabulary; here the route (`POST …/commands/{command}`) merely validates
its path segment against this tuple.

## The order every adapter takes, written once

:func:`ingest` is refuse-cold → admit → execute, in the caller's ONE
transaction. `webhook_ingress` states the rule — *a delivery that cannot be
executed is refused BEFORE admission, never after* — and predicted that the
second channel would be the moment to stop retyping it per adapter. The web
adapter is that channel, so the ordering lives here: an unknown name is
refused before any dedup row exists, admission (`command_dedup`, keyed by
the adapter's idempotency reference) precedes execution, and because all of
it shares the adapter's transaction a refusal anywhere rolls the dedup row
back and a retry re-executes. What stays with the adapter is only the shape
of its key — a web ``Idempotency-Key``, a Telegram ``update_id`` — and its
transport answers (R5's "acknowledge fast" is not this module's).

## Not-built is a named refusal, not an absent name

A vocabulary command without an executor yet answers :class:`CommandNotBuilt`
(the adapter renders it 501, naming the command). It is NOT dropped from the
enum: the surface must advertise the contract it implements, and the set of
unbuilt names is pinned by the test so it can only shrink deliberately. The
gate runs BEFORE the not-built check, so a non-member cannot enumerate the
surface on a workspace they cannot see (`07` §5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional

from src.exceptions.base import StorydumpError
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import tenant_resolution, webhook_ingress
from src.services.target.workspaces import InvalidWorkspaceArgs

#: The closed inbound vocabulary — `01` §Interaction-layer port, verbatim
#: order. Set-equality with the doc is asserted by the unit gate.
VOCABULARY: tuple[str, ...] = (
    "approve",
    "skip",
    "reject",
    "mark_posted",
    "cancel",
    "autopost_now",
    "sync_now",
    "settings_change",
    "pause_workspace",
    "resume_workspace",
    "connect_account",
    "reconnect_account",
    "disconnect_account",
    "move_account",
    "create_workspace",
    "rename_workspace",
    "offboard_workspace",
    "restore_workspace",
    "invite_member",
    "remove_member",
    "change_role",
    "transfer_ownership",
    "resolve_review",
    "clear_quarantine",
)

#: The floor ladder. ``user`` = any active signed-in user, no membership
#: (the workspace does not exist yet); ``member``/``admin``/``owner`` = the
#: `workspace_members` ladder `tenant_resolution.ROLE_ORDER` enforces;
#: ``operator`` = a `service_tokens` bearer (`07` §6, X.2) — no user principal
#: can satisfy it, and the refusal is a ROLE refusal so members are not told
#: which operator commands exist.
FLOORS: tuple[str, ...] = ("user", "member", "admin", "owner", "operator")

#: Per-command floor, from `06` §2 (membership), §4 (accounts), §5 (operator).
ROLE_FLOOR: dict[str, str] = {
    "approve": "member",
    "skip": "member",
    "reject": "member",
    "mark_posted": "member",
    "cancel": "member",
    "autopost_now": "member",
    "sync_now": "member",
    "settings_change": "admin",
    "pause_workspace": "admin",
    "resume_workspace": "admin",
    "connect_account": "admin",
    "reconnect_account": "admin",
    "disconnect_account": "admin",
    "move_account": "admin",
    "create_workspace": "user",
    "rename_workspace": "admin",
    "offboard_workspace": "owner",
    "restore_workspace": "owner",
    "invite_member": "admin",
    "remove_member": "admin",
    "change_role": "admin",
    "transfer_ownership": "owner",
    "resolve_review": "operator",
    "clear_quarantine": "operator",
}

#: `CommandRefused.reason`, closed. Adapters map it without parsing prose, and
#: the web adapter's status table is pinned TOTAL over this tuple.
REASONS: tuple[str, ...] = (
    "unknown_command",
    "not_built",
    "workspace_required",
    "invalid_args",
    "illegal_transition",
    "not_found",
    "manual_mode",
)


@dataclass(frozen=True)
class Command:
    """One normalized inbound command. Resolved ids only — never a chat id,
    a session value or a transport payload."""

    kind: str
    workspace_id: Optional[str]
    actor_user_id: str
    channel: str
    args: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    """``outcome`` is ``executed`` (an inline state flip happened) or
    ``enqueued`` (a job row now carries the work); ``data`` is what the
    adapter renders."""

    outcome: str
    data: dict


class CommandRefused(StorydumpError):
    """A command the port will not run, with a reason from :data:`REASONS`."""

    def __init__(self, reason: str, detail: str = ""):
        if reason not in REASONS:
            raise ValueError(f"not a refusal reason: {reason!r}")
        self.reason = reason
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
            "sync_now": ex.sync_now,
            # gdrive epic P4 — the trio is provider-general (F1 (a)) and
            # THIN: the OAuth leg is the API route's, these initiate and record.
            "connect_account": ex.connect_account,
            "reconnect_account": ex.reconnect_account,
            "disconnect_account": ex.disconnect_account,
            "settings_change": ex.settings_change,
            "pause_workspace": ex.pause_workspace,
            "resume_workspace": ex.resume_workspace,
            "create_workspace": ex.create_workspace,
            "rename_workspace": ex.rename_workspace,
            # `06` §2's invited path. The ACCEPT half has been built since
            # `fn_invitation_accept` landed, so until now the surface could
            # consume an invitation nobody could create (#1090 G1).
            "invite_member": ex.invite_member,
        }
    )
    return registry


#: kind → executor, or None when the executor is not built. Total over the
#: vocabulary by construction; the None set is pinned by the unit gate.
REGISTRY: dict[str, Optional[Executor]] = _build_registry()

#: The named not-built set — shrinks deliberately, in the PR that builds one.
UNBUILT: tuple[str, ...] = tuple(k for k in VOCABULARY if REGISTRY[k] is None)


async def execute(session, command: Command) -> CommandResult:
    """Gate, then execute. The ONE path every adapter takes.

    *session* is the caller's open unit of work (tenant + actor GUCs already
    applied — `02` §0's writer-identity rule, enforced by the audit triggers).

    Order is load-bearing: unknown → refused cold (no gate, nothing to
    authorize against); then the gate; then not-built; then the executor. A
    refusal from the gate propagates as the `TenantResolutionError` it is —
    the adapter already maps that type (not-a-member → 404, below-floor → 403).
    A caller-supplied value the boundary refuses (`InvalidWorkspaceArgs`, from
    the writers or from a database CHECK) is one refusal, mapped here once.
    """
    if command.kind not in ROLE_FLOOR:
        raise UnknownCommand(command.kind)
    floor = ROLE_FLOOR[command.kind]

    if floor == "operator":
        # No user principal satisfies an operator floor. Refused as a ROLE
        # refusal — deliberately not `CommandNotBuilt` — so the surface never
        # confirms to a member that an operator command exists (`07` §5).
        raise TenantResolutionError(
            "insufficient_role", f"{command.kind} requires an operator principal"
        )
    if floor != "user":
        if not command.workspace_id:
            raise CommandRefused("workspace_required", command.kind)
        await tenant_resolution.authorize_member(
            session, command.workspace_id, command.actor_user_id, floor
        )

    executor = REGISTRY.get(command.kind)
    if executor is None:
        raise CommandNotBuilt(command.kind)
    try:
        return await executor(session, command)
    except InvalidWorkspaceArgs as exc:
        raise CommandRefused("invalid_args", str(exc)) from exc


async def ingest(
    session, command: Command, *, external_ref: str, principal: str, payload: Any
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
