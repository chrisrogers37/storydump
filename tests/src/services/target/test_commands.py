"""The interaction-layer port (`01` §Interaction-layer port, FC-2) — unit gate.

Four properties are pinned here, each for a reason that is NOT "the code
should match the doc":

1. **The vocabulary equals the normative list, in both directions.** `01:49`
   says the list "is its normative home, and adding a command is a deliberate
   change here, ratchet-visible." A per-command route table would make the
   ROUTES the vocabulary and the doc a description of them; parsing the doc
   makes the doc the authority and the enum a copy that cannot drift silently
   — in EITHER direction, which is why the assertion is set-equality rather
   than a subset check.
2. **Every command has a role floor.** The gate is one function; a command it
   has no floor for would be refused by construction, so the table's totality
   is what stands between "one central gate" and "one central gate plus a
   KeyError".
3. **The not-built set is named.** A vocabulary command without an executor
   answers "not built" rather than being missing from the enum. Pinning the set
   means it can only shrink deliberately; a new executor that forgets to leave
   the tuple reddens this test, which is the visibility the doc asks for.
4. **`ingest` owns the order** — refuse cold, admit, execute — so no adapter
   re-types it. The seams are the module attributes the port calls through
   (`tenant_resolution.authorize_member`, `webhook_ingress.admit`, `REGISTRY`),
   patched the way the rest of the tier's tests patch theirs.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from src.exceptions.tenancy import TenantResolutionError
from src.services.target import commands as port
from src.services.target import tenant_resolution, webhook_ingress
from src.services.target.commands import (
    Command,
    CommandNotBuilt,
    CommandRefused,
    CommandResult,
    UnknownCommand,
    execute,
    ingest,
)
from src.services.target.webhook_ingress import DeliveryReplayed
from src.services.target.workspaces import InvalidWorkspaceArgs

DOC = (
    pathlib.Path(__file__).resolve().parents[4]
    / "documentation/planning/2026-08-02-consolidated-design-plan/01-target-architecture.md"
)


def _doc_vocabulary() -> set[str]:
    """Every backticked identifier in the `01` inbound bullet.

    Anchored to the bullet that opens with "**Inbound:**" rather than a line
    number, so a doc edit above it does not move the goalposts; the identifiers
    are the backticked words that look like command names (lowercase + underscore).
    """
    text = DOC.read_text()
    m = re.search(r"\*\*Inbound:\*\*(.*?)\n", text)
    assert m, "the `01` inbound bullet moved or was renamed — re-anchor the parser"
    names = set(re.findall(r"`([a-z_]+)`", m.group(1)))
    # The bullet also names the table the vocabulary is enforced by; that is
    # not a command. Anything else backticked in that sentence IS one.
    return names - {"src/services/core/**"}


class TestVocabularyIsTheDocs:
    def test_set_equality_with_01_in_both_directions(self):
        doc = _doc_vocabulary()
        code = set(port.VOCABULARY)
        assert code == doc, (
            f"only in code: {sorted(code - doc)}; only in doc: {sorted(doc - code)}"
        )

    def test_the_doc_parser_is_not_vacuous(self):
        """A parser that matched nothing would make equality trivially false,
        so this is not a tautology guard; it exists so a doc rewrite that
        empties the match shows up as 'parser broke', not 'vocabulary broke'."""
        assert len(_doc_vocabulary()) >= 20

    def test_no_duplicates_and_stable_order(self):
        assert len(port.VOCABULARY) == len(set(port.VOCABULARY))


class TestEveryCommandHasAFloorAndAnExecutorSlot:
    def test_role_floor_is_total(self):
        assert set(port.ROLE_FLOOR) == set(port.VOCABULARY)

    def test_floors_are_the_closed_ladder(self):
        assert set(port.ROLE_FLOOR.values()) <= set(port.FLOORS)

    def test_registry_is_total(self):
        assert set(port.REGISTRY) == set(port.VOCABULARY)

    def test_the_not_built_set_is_exactly_this(self):
        """Shrinks deliberately. Building an executor = remove its name here
        in the same PR, which is the visible edit the doc asks for."""
        assert set(port.UNBUILT) == {
            "autopost_now",
            "connect_account",
            "reconnect_account",
            "disconnect_account",
            "move_account",
            "offboard_workspace",
            "restore_workspace",
            "invite_member",
            "remove_member",
            "change_role",
            "transfer_ownership",
            "resolve_review",
            "clear_quarantine",
        }
        for name in port.UNBUILT:
            assert port.REGISTRY[name] is None

    def test_built_executors_are_callables(self):
        for name, fn in port.REGISTRY.items():
            if name not in port.UNBUILT:
                assert callable(fn), name

    def test_a_refusal_reason_outside_the_closed_set_is_a_programming_error(self):
        with pytest.raises(ValueError):
            CommandRefused("because")


def _cmd(kind="approve", **args) -> Command:
    return Command(
        kind=kind,
        workspace_id="ws-1",
        actor_user_id="user-1",
        channel="web",
        args=args,
    )


class _Session:
    """A stand-in for the UoW session: the port never touches it directly,
    it only hands it to the gate and the executor."""


@pytest.fixture
def gate(monkeypatch):
    """The central gate, recording what it was asked; ``gate.refuse`` makes
    it raise, ``gate.role`` is what it answers."""

    class Log(list):
        refuse = None
        role = "owner"

    log = Log()

    async def authorize_member(session, ws, user, minimum_role="member"):
        log.append((ws, user, minimum_role))
        if log.refuse is not None:
            raise log.refuse
        return log.role

    monkeypatch.setattr(tenant_resolution, "authorize_member", authorize_member)
    return log


@pytest.fixture
def executor(monkeypatch):
    """`approve` (and `settings_change`) answered by a recording executor."""
    seen = []

    async def run(session, command):
        seen.append(command)
        return CommandResult("executed", {"state": "approved"})

    monkeypatch.setitem(port.REGISTRY, "approve", run)
    monkeypatch.setitem(port.REGISTRY, "settings_change", run)
    return seen


class TestExecuteIsTheOnePath:
    async def test_unknown_command_is_refused_before_anything_runs(self, gate):
        with pytest.raises(UnknownCommand):
            await execute(_Session(), _cmd(kind="frobnicate"))
        assert gate == []

    async def test_not_built_is_named_and_refused_AFTER_the_gate(self, gate):
        """The gate runs first on purpose: a non-member must not learn which
        commands exist by probing 501s on a workspace they cannot see."""
        with pytest.raises(CommandNotBuilt) as exc:
            await execute(_Session(), _cmd(kind="transfer_ownership"))
        assert exc.value.command == "transfer_ownership"
        assert gate == [("ws-1", "user-1", "owner")]

    async def test_gate_refusal_propagates_and_the_executor_never_runs(
        self, gate, executor
    ):
        gate.refuse = TenantResolutionError("not_a_member")
        with pytest.raises(TenantResolutionError):
            await execute(_Session(), _cmd())
        assert executor == []

    async def test_the_executor_receives_the_command_and_its_result_is_returned(
        self, gate, executor
    ):
        out = await execute(_Session(), _cmd(intent_id="i-1"))
        assert out == CommandResult("executed", {"state": "approved"})
        assert executor[0].args == {"intent_id": "i-1"}

    async def test_the_gate_is_asked_for_the_commands_own_floor(self, gate, executor):
        await execute(_Session(), _cmd(kind="settings_change"))
        await execute(_Session(), _cmd(kind="approve"))
        assert [asked[2] for asked in gate] == ["admin", "member"]

    async def test_create_workspace_has_no_membership_to_gate(self, gate, monkeypatch):
        """The one command with no workspace yet: the gate is NOT consulted,
        because there is no workspace_members row to consult."""

        async def run(session, command):
            return CommandResult("executed", {"workspace_id": "ws-new"})

        monkeypatch.setitem(port.REGISTRY, "create_workspace", run)
        cmd = Command(
            kind="create_workspace",
            workspace_id=None,
            actor_user_id="user-1",
            channel="web",
            args={"name": "Mine"},
        )
        out = await execute(_Session(), cmd)
        assert gate == []
        assert out.data == {"workspace_id": "ws-new"}

    async def test_a_workspace_command_without_a_workspace_is_refused(self, gate):
        cmd = Command(
            kind="approve", workspace_id=None, actor_user_id="u", channel="web", args={}
        )
        with pytest.raises(CommandRefused) as exc:
            await execute(_Session(), cmd)
        assert exc.value.reason == "workspace_required"
        assert gate == []

    async def test_operator_floor_refuses_a_user_principal(self, gate):
        """`resolve_review`/`clear_quarantine` are operator-only (`07` §6).
        Until service tokens exist no principal can satisfy the floor, and the
        refusal must be a ROLE refusal — not a 'not built' — so the surface
        does not advertise operator commands to members."""
        with pytest.raises(TenantResolutionError) as exc:
            await execute(_Session(), _cmd(kind="clear_quarantine"))
        assert exc.value.reason == "insufficient_role"
        assert gate == [], "the membership gate must not be asked for an operator floor"

    async def test_a_writer_refusal_is_mapped_once_here(self, gate, monkeypatch):
        async def run(session, command):
            raise InvalidWorkspaceArgs("tz is not a zone")

        monkeypatch.setitem(port.REGISTRY, "settings_change", run)
        with pytest.raises(CommandRefused) as exc:
            await execute(_Session(), _cmd(kind="settings_change"))
        assert exc.value.reason == "invalid_args"


@pytest.fixture
def admission(monkeypatch):
    """`webhook_ingress.admit` recorded; ``admission.refuse`` makes it raise."""

    class Log(list):
        refuse = None

    log = Log()

    async def admit(session, *, channel, external_ref, payload, principal):
        log.append((channel, external_ref, payload, principal))
        if log.refuse is not None:
            raise log.refuse

    monkeypatch.setattr(webhook_ingress, "admit", admit)
    return log


class TestIngestOwnsTheOrder:
    async def test_unknown_is_refused_before_admission(self, admission, gate):
        with pytest.raises(UnknownCommand):
            await ingest(
                _Session(),
                _cmd(kind="frobnicate"),
                external_ref="k-1",
                principal="sess-1",
                payload={},
            )
        assert admission == [] and gate == []

    async def test_admits_under_the_commands_channel_then_executes(
        self, admission, gate, executor
    ):
        out = await ingest(
            _Session(),
            _cmd(intent_id="i-1"),
            external_ref="k-1",
            principal="sess-1",
            payload={"intent_id": "i-1"},
        )
        assert admission == [("web", "k-1", {"intent_id": "i-1"}, "sess-1")]
        assert gate == [("ws-1", "user-1", "member")]
        assert out.outcome == "executed"

    async def test_a_replay_propagates_and_nothing_executes(
        self, admission, gate, executor
    ):
        admission.refuse = DeliveryReplayed("same key, same body")
        with pytest.raises(DeliveryReplayed):
            await ingest(
                _Session(), _cmd(), external_ref="k-1", principal="sess-1", payload={}
            )
        assert gate == [] and executor == []
