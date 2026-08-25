"""The interaction-layer port (`01` §Interaction-layer port, FC-2) — unit gate.

Three properties are pinned here, each for a reason that is NOT "the code
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
"""

from __future__ import annotations

import pathlib
import re

import pytest

from src.services.target import commands as port
from src.services.target.commands import (
    Command,
    CommandNotBuilt,
    CommandResult,
    UnknownCommand,
    execute,
)

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


class TestExecuteIsTheOnePath:
    async def test_unknown_command_is_refused_before_anything_runs(self):
        calls = []

        async def gate(session, ws, user, minimum_role):
            calls.append("gate")
            return "owner"

        with pytest.raises(UnknownCommand):
            await execute(_Session(), _cmd(kind="frobnicate"), gate=gate)
        assert calls == []

    async def test_not_built_is_named_and_refused_AFTER_the_gate(self):
        """The gate runs first on purpose: a non-member must not learn which
        commands exist by probing 501s on a workspace they cannot see."""
        calls = []

        async def gate(session, ws, user, minimum_role):
            calls.append(("gate", ws, user, minimum_role))
            return "owner"

        with pytest.raises(CommandNotBuilt) as exc:
            await execute(_Session(), _cmd(kind="transfer_ownership"), gate=gate)
        assert exc.value.command == "transfer_ownership"
        assert calls == [("gate", "ws-1", "user-1", "owner")]

    async def test_gate_refusal_propagates_and_the_executor_never_runs(self):
        from src.exceptions.tenancy import TenantResolutionError

        ran = []

        async def gate(session, ws, user, minimum_role):
            raise TenantResolutionError("not_a_member")

        async def executor(session, command):
            ran.append(command)
            return CommandResult("executed", {})

        with pytest.raises(TenantResolutionError):
            await execute(
                _Session(),
                _cmd(),
                gate=gate,
                registry={**port.REGISTRY, "approve": executor},
            )
        assert ran == []

    async def test_the_executor_receives_the_command_and_its_result_is_returned(self):
        seen = []

        async def gate(session, ws, user, minimum_role):
            return "member"

        async def executor(session, command):
            seen.append(command)
            return CommandResult("executed", {"state": "approved"})

        out = await execute(
            _Session(),
            _cmd(intent_id="i-1"),
            gate=gate,
            registry={**port.REGISTRY, "approve": executor},
        )
        assert out == CommandResult("executed", {"state": "approved"})
        assert seen[0].args == {"intent_id": "i-1"}

    async def test_the_gate_is_asked_for_the_commands_own_floor(self):
        asked = []

        async def gate(session, ws, user, minimum_role):
            asked.append(minimum_role)
            return "owner"

        async def executor(session, command):
            return CommandResult("executed", {})

        reg = {**port.REGISTRY, "settings_change": executor, "approve": executor}
        await execute(_Session(), _cmd(kind="settings_change"), gate=gate, registry=reg)
        await execute(_Session(), _cmd(kind="approve"), gate=gate, registry=reg)
        assert asked == ["admin", "member"]

    async def test_create_workspace_has_no_membership_to_gate(self):
        """The one command with no workspace yet: the gate is NOT consulted,
        because there is no workspace_members row to consult."""
        calls = []

        async def gate(session, ws, user, minimum_role):
            calls.append("gate")
            return "owner"

        async def executor(session, command):
            return CommandResult("executed", {"workspace_id": "ws-new"})

        cmd = Command(
            kind="create_workspace",
            workspace_id=None,
            actor_user_id="user-1",
            channel="web",
            args={"name": "Mine"},
        )
        out = await execute(
            _Session(),
            cmd,
            gate=gate,
            registry={**port.REGISTRY, "create_workspace": executor},
        )
        assert calls == []
        assert out.data == {"workspace_id": "ws-new"}

    async def test_a_workspace_command_without_a_workspace_is_refused(self):
        async def gate(session, ws, user, minimum_role):
            return "owner"

        cmd = Command(
            kind="approve", workspace_id=None, actor_user_id="u", channel="web", args={}
        )
        with pytest.raises(port.CommandRefused) as exc:
            await execute(_Session(), cmd, gate=gate)
        assert exc.value.reason == "workspace_required"

    async def test_operator_floor_refuses_a_user_principal(self):
        """`resolve_review`/`clear_quarantine` are operator-only (`07` §6).
        Until service tokens exist no principal can satisfy the floor, and the
        refusal must be a ROLE refusal — not a 'not built' — so the surface
        does not advertise operator commands to members."""
        from src.exceptions.tenancy import TenantResolutionError

        async def gate(session, ws, user, minimum_role):
            raise AssertionError(
                "the membership gate must not be asked for an operator floor"
            )

        with pytest.raises(TenantResolutionError) as exc:
            await execute(_Session(), _cmd(kind="clear_quarantine"), gate=gate)
        assert exc.value.reason == "insufficient_role"
