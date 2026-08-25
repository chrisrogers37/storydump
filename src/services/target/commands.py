"""The interaction-layer port (`01` §Interaction-layer port, FC-2; task #1028).

Every inbound channel — the web/API adapter today, the Telegram webhook (W4)
next — normalizes what it received into a :class:`Command` and hands it to
:func:`execute`. Nothing inland sees a chat id, a session cookie, a callback
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

## What lives here and what deliberately does not

- **Here:** the vocabulary, the role floor per command (`06` §2/§4/§5's actor
  tables, collapsed to the `workspace_members` ladder), the executor registry,
  and :func:`execute` — which runs the ONE central authorization gate
  (`tenant_resolution.authorize_member`) and then the executor, inside the
  caller's unit of work.
- **Not here:** admission. `command_dedup` is written by the ADAPTER before it
  calls this (`webhook_ingress.admit`, `02` §6), because the idempotency key's
  shape is channel-specific — a Telegram `update_id`, a web `Idempotency-Key`
  header, a CLI token — and only the adapter holds it. Both adapters follow the
  same order: refuse-before-admit, admit-before-execute, all in one transaction
  so a failed command leaves no dedup row behind and a retry re-executes.
- **Not here either:** transport answers. R5's "acknowledge fast" is the
  adapter's; this function returns a :class:`CommandResult` and never speaks
  HTTP or Telegram.

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
from src.services.target.tenant_resolution import authorize_member

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
    """A command the port will not run. ``reason`` is a closed vocabulary so
    adapters map it without parsing prose: ``unknown_command`` ·
    ``not_built`` · ``workspace_required`` · ``invalid_args`` ·
    ``illegal_transition`` · ``not_found`` · ``manual_mode``."""

    def __init__(self, reason: str, detail: str = ""):
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
Gate = Callable[[Any, str, str, str], Awaitable[str]]


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
            "settings_change": ex.settings_change,
            "pause_workspace": ex.pause_workspace,
            "resume_workspace": ex.resume_workspace,
            "create_workspace": ex.create_workspace,
            "rename_workspace": ex.rename_workspace,
        }
    )
    return registry


#: kind → executor, or None when the executor is not built. Total over the
#: vocabulary by construction; the None set is pinned by the unit gate.
REGISTRY: dict[str, Optional[Executor]] = _build_registry()

#: The named not-built set — shrinks deliberately, in the PR that builds one.
UNBUILT: tuple[str, ...] = tuple(k for k in VOCABULARY if REGISTRY[k] is None)


async def execute(
    session,
    command: Command,
    *,
    gate: Gate = authorize_member,
    registry: Optional[Mapping[str, Optional[Executor]]] = None,
) -> CommandResult:
    """Gate, then execute. The ONE path every adapter takes.

    *session* is the caller's open unit of work (tenant + actor GUCs already
    applied — `02` §0's writer-identity rule, enforced by the audit triggers).
    *gate* and *registry* are seams for the unit gate; production callers pass
    neither.

    Order is load-bearing: unknown → refused cold (no gate, nothing to
    authorize against); then the gate; then not-built; then the executor. A
    refusal from the gate propagates as the `TenantResolutionError` it is —
    the adapter already maps that type (not-a-member → 404, below-floor → 403).
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
        await gate(session, command.workspace_id, command.actor_user_id, floor)

    reg = REGISTRY if registry is None else registry
    executor = reg.get(command.kind)
    if executor is None:
        raise CommandNotBuilt(command.kind)
    return await executor(session, command)
