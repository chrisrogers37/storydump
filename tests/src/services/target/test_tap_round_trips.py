"""#1286 — a tap's round trips: the pieces the dispatch no longer spends one
statement each on. The dispatch-level count is in `test_telegram_dispatch.py`
(`TestATapIsCheap`); the real number is the tap gate's statement budget."""

from __future__ import annotations

import pytest

from src.exceptions.tenancy import TenantResolutionError
from src.services.target import (
    command_executors,
    identity,
    outbox,
    tenant_resolution,
)
from src.services.target.commands import Command


class _Ex:
    """An executor double answering one row and recording statements."""

    def __init__(self, row=None):
        self.row = row
        self.statements = []

    async def execute(self, stmt, params=None):
        self.statements.append((" ".join(str(stmt).split()), params))
        row = self.row

        class _R:
            def first(self_inner):
                return row

            def mappings(self_inner):
                class _M:
                    def first(self_m):
                        return row

                return _M()

        return _R()


class TestTheTapperInOneRead:
    async def test_the_identity_and_its_name_come_back_together(self):
        """One read, and the person's state rides in it too (#1572): the
        refusal of a disabled tapper costs no second statement."""
        ex = _Ex(row=("u-1", "Dana", "active"))
        got = await identity.tapper_for_identity(
            ex, provider="telegram", external_id="555"
        )
        assert got == ("u-1", "Dana")
        assert len(ex.statements) == 1
        sql, params = ex.statements[0]
        assert (
            "SELECT i.user_id, i.display_name, u.state"
            " FROM user_identities i JOIN users u ON u.id = i.user_id" in sql
        )
        assert params == {"p": "telegram", "sub": "555"}

    async def test_an_unlinked_identity_is_none(self):
        assert (
            await identity.tapper_for_identity(
                _Ex(row=None), provider="telegram", external_id="555"
            )
            is None
        )

    async def test_an_empty_name_is_none_so_the_long_way_is_asked(self):
        got = await identity.tapper_for_identity(
            _Ex(row=("u-1", "", "active")), provider="telegram", external_id="555"
        )
        assert got == ("u-1", None)


class TestTheActorNameOnTheCommand:
    async def test_a_label_on_the_command_needs_no_read(self, monkeypatch):
        async def never(executor, *, user_id):  # pragma: no cover
            raise AssertionError("the label was on the command")

        monkeypatch.setattr(identity, "display_name_for", never)
        assert (
            await command_executors._actor_name(object(), "u-1", label="Dana") == "Dana"
        )

    async def test_without_a_label_the_identity_table_is_asked(self, monkeypatch):
        asked = []

        async def display_name_for(executor, *, user_id):
            asked.append(user_id)
            return "Chris"

        monkeypatch.setattr(identity, "display_name_for", display_name_for)
        assert await command_executors._actor_name(object(), "u-1") == "Chris"
        assert asked == ["u-1"]

    async def test_no_actor_is_no_name(self):
        assert await command_executors._actor_name(object(), None) is None

    async def test_the_outcome_line_takes_the_label_from_the_command(self, monkeypatch):
        """`_record_outcome` hands the tap's label through — the only place
        the second identity read used to happen."""
        seen = {}

        async def _actor_name(session, user_id, *, label=None):
            seen["args"] = (user_id, label)
            return label or "?"

        async def supersede(session, **kw):
            return 1

        monkeypatch.setattr(command_executors, "_actor_name", _actor_name)
        monkeypatch.setattr(command_executors, "_supersede_everywhere", supersede)
        command = Command(
            kind="skip",
            workspace_id="ws",
            actor_user_id="u-1",
            channel="telegram",
            args={"intent_id": "i"},
            actor_label="Dana",
        )
        line = await command_executors._record_outcome(
            object(), {"id": "i", "eff_tz": "UTC"}, command, "skipped"
        )
        assert seen["args"] == ("u-1", "Dana") and "Dana" in line

    async def test_a_label_in_args_is_never_honoured(self, monkeypatch):
        """The web route passes the request body into `args` verbatim; a name
        there would let a member choose what the group reads after "by"."""
        seen = {}

        async def _actor_name(session, user_id, *, label=None):
            seen["args"] = (user_id, label)
            return "Chris"

        async def supersede(session, **kw):
            return 1

        monkeypatch.setattr(command_executors, "_actor_name", _actor_name)
        monkeypatch.setattr(command_executors, "_supersede_everywhere", supersede)
        command = Command(
            kind="skip",
            workspace_id="ws",
            actor_user_id="u-1",
            channel="web",
            args={"intent_id": "i", "actor_label": "The CEO"},
        )
        line = await command_executors._record_outcome(
            object(), {"id": "i", "eff_tz": "UTC"}, command, "skipped"
        )
        assert seen["args"] == ("u-1", None) and "The CEO" not in line


class TestTheGateIsOneRead:
    """The gate reads under its caller's tenant and sets none (#1632): one
    statement, the membership read binding both keys, whatever it decides."""

    @pytest.mark.parametrize(
        "role,floor,refusal",
        [
            ("member", "member", None),
            ("admin", "member", None),
            ("owner", "member", None),
            ("member", "admin", "insufficient_role"),
            ("admin", "admin", None),
            ("owner", "admin", None),
            ("member", "owner", "insufficient_role"),
            ("admin", "owner", "insufficient_role"),
            ("owner", "owner", None),
            (None, "member", "not_a_member"),
            (None, "admin", "not_a_member"),
            (None, "owner", "not_a_member"),
        ],
    )
    async def test_every_role_at_every_floor(self, role, floor, refusal):
        ex = _Ex(row=None if role is None else (role,))
        try:
            got = await tenant_resolution.authorize_member(ex, "ws-1", "u-1", floor)
            refused = None
        except TenantResolutionError as exc:
            got, refused = None, exc.reason
        assert refused == refusal
        assert got == (None if refusal else role)
        assert len(ex.statements) == 1, "the gate sets no tenant: it only reads"
        sql, params = ex.statements[0]
        assert "FROM workspace_members WHERE workspace_id = :ws AND user_id = :u" in sql
        assert params == {"ws": "ws-1", "u": "u-1"}


class TestTheSupersedeIsOneStatement:
    async def test_every_binding_s_cards_and_their_edits_in_one_round_trip(self):
        ex = _Ex(row=(2, 2))
        n = await outbox.supersede_everywhere(
            object.__new__(_Ex) if False else ex,
            workspace_id="ws-1",
            intent_id="i-1",
            outcome_text="✅ Approved by Dana",
        )
        assert n == 2
        assert len(ex.statements) == 1
        sql, params = ex.statements[0]
        assert params == {"ws": "ws-1", "i": "i-1", "o": "✅ Approved by Dana"}
        for piece in (
            "FROM channel_bindings",
            "state = 'active'",
            "channel LIKE 'telegram%'",
            "UPDATE channel_outbox o SET state = 'superseded'",
            "kind IN ('approval_prompt', 'invitation')",
            "state IN ('pending', 'sending', 'sent', 'ambiguous')",
            "RETURNING o.binding_id, o.external_message_ref, o.payload",
            "'prompt_supersede'",
            "jsonb_strip_nulls(jsonb_build_object(",
            "'supersedes_ref', s.external_message_ref",
            "'header', COALESCE(NULLIF(s.payload->>'caption', ''), NULLIF(s.payload->>'text', ''))",
            "'sent_as', NULLIF(s.payload->>'sent_as', '')",
            "WHERE s.external_message_ref IS NOT NULL",
            "(SELECT count(*) FROM sup) AS superseded",
        ):
            assert piece in sql, piece

    async def test_no_live_card_is_zero(self):
        ex = _Ex(row=(0, 0))
        assert (
            await outbox.supersede_everywhere(
                ex, workspace_id="ws-1", intent_id="i-1", outcome_text=None
            )
            == 0
        )
