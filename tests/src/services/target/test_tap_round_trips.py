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
        ex = _Ex(row=("u-1", "Dana"))
        got = await identity.tapper_for_identity(
            ex, provider="telegram", external_id="555"
        )
        assert got == ("u-1", "Dana")
        assert len(ex.statements) == 1
        sql, params = ex.statements[0]
        assert "SELECT user_id, display_name FROM user_identities" in sql
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
            _Ex(row=("u-1", "")), provider="telegram", external_id="555"
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
            args={"intent_id": "i", "actor_label": "Dana"},
        )
        line = await command_executors._record_outcome(
            object(), {"id": "i", "eff_tz": "UTC"}, command, "skipped"
        )
        assert seen["args"] == ("u-1", "Dana") and "Dana" in line


class TestTheGateTrustsABoundTenant:
    async def test_tenant_bound_skips_the_second_guc_set(self, monkeypatch):
        applied = []

        async def apply_gucs(executor, **kw):
            applied.append(kw)

        monkeypatch.setattr(tenant_resolution, "apply_gucs", apply_gucs)
        ex = _Ex(row=("admin",))
        role = await tenant_resolution.authorize_member(
            ex, "ws-1", "u-1", "member", tenant_bound=True
        )
        assert role == "admin"
        assert applied == [], "the caller bound the tenant; the gate did not re-set it"
        assert len(ex.statements) == 1 and "workspace_members" in ex.statements[0][0]

    async def test_by_default_the_gate_binds_the_claim_itself(self, monkeypatch):
        applied = []

        async def apply_gucs(executor, **kw):
            applied.append(kw)

        monkeypatch.setattr(tenant_resolution, "apply_gucs", apply_gucs)
        await tenant_resolution.authorize_member(_Ex(row=("member",)), "ws-1", "u-1")
        assert applied == [{"tenant_id": "ws-1"}]

    async def test_the_where_still_binds_both_keys_when_bound(self, monkeypatch):
        async def apply_gucs(executor, **kw):  # pragma: no cover
            raise AssertionError("not called")

        monkeypatch.setattr(tenant_resolution, "apply_gucs", apply_gucs)
        ex = _Ex(row=None)
        with pytest.raises(TenantResolutionError) as info:
            await tenant_resolution.authorize_member(
                ex, "ws-1", "u-1", "member", tenant_bound=True
            )
        assert info.value.reason == "not_a_member"
        assert ex.statements[0][1] == {"ws": "ws-1", "u": "u-1"}


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
