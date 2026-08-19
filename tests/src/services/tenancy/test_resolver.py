"""Adapter tests for the one tenant resolver (F.3, #842).

`04` F.3's gate names these: *"adapter tests cover resolution."* Both inbound
shapes, every missing-tenant policy, and — the part a vacuity check cannot
reach — that the resolver returns the tenant belonging to the identity it was
handed rather than merely returning *a* tenant.

That last class is why the fake repository below holds TWO tenants. A
single-tenant fixture makes "resolved something" and "resolved the right thing"
the same assertion, so a resolver that ignored its argument entirely would pass
it. Not tautological, just about the wrong subject — the shape rajan caught on
#852, where a test meant the owner and asserted the admin.
"""

from __future__ import annotations

import pytest

from src.services.tenancy import (
    ABSENT,
    PROVISION,
    REFUSE,
    TenantResolutionError,
    resolve_tenant_from_chat_id,
    resolve_tenant_from_web_session,
)

#: Two real tenants, so every assertion can name which one it expected.
CHAT_A, TENANT_A = 111, "aaaaaaaa-0000-0000-0000-000000000001"
CHAT_B, TENANT_B = 222, "bbbbbbbb-0000-0000-0000-000000000002"
UNKNOWN_CHAT = 999


class _Row:
    def __init__(self, tenant_id):
        self.id = tenant_id


class FakeSettingsRepo:
    """Records what it was asked, so provisioning can be asserted, not assumed."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {CHAT_A: TENANT_A, CHAT_B: TENANT_B})
        self.created = []

    def get_by_chat_id(self, telegram_chat_id):
        found = self.rows.get(telegram_chat_id)
        return _Row(found) if found else None

    def get_or_create(self, telegram_chat_id):
        if telegram_chat_id not in self.rows:
            self.rows[telegram_chat_id] = f"created-{telegram_chat_id}"
            self.created.append(telegram_chat_id)
        return _Row(self.rows[telegram_chat_id])


class TestItResolvesTheRightTenantNotJustATenant:
    """Wrong-subject gating. Each asserts the tenant for the identity passed."""

    @pytest.mark.parametrize("chat,expected", [(CHAT_A, TENANT_A), (CHAT_B, TENANT_B)])
    def test_a_chat_id_resolves_to_ITS_OWN_tenant(self, chat, expected):
        repo = FakeSettingsRepo()
        got = resolve_tenant_from_chat_id(chat, settings_repo=repo)
        assert got.tenant_id == expected

    def test_the_two_chats_do_not_resolve_to_the_same_tenant(self):
        """The positive control on the parametrise above: if the resolver
        ignored its argument, both cases would still pass individually."""
        repo = FakeSettingsRepo()
        a = resolve_tenant_from_chat_id(CHAT_A, settings_repo=repo).tenant_id
        b = resolve_tenant_from_chat_id(CHAT_B, settings_repo=repo).tenant_id
        assert a != b and {a, b} == {TENANT_A, TENANT_B}

    def test_a_web_session_resolves_to_ITS_OWN_tenant(self):
        repo = FakeSettingsRepo()
        got = resolve_tenant_from_web_session({"chat_id": CHAT_B}, settings_repo=repo)
        assert got.tenant_id == TENANT_B


class TestBothInboundShapes:
    def test_the_chat_shape_reports_its_origin(self):
        repo = FakeSettingsRepo()
        assert (
            resolve_tenant_from_chat_id(CHAT_A, settings_repo=repo).origin
            == "telegram_chat"
        )

    def test_the_web_shape_reports_ITS_origin_even_though_the_lookup_is_shared(self):
        """The surface a request arrived on is a fact audits care about, and it
        must not be erased by the two shapes sharing one lookup today."""
        repo = FakeSettingsRepo()
        assert (
            resolve_tenant_from_web_session(
                {"chat_id": CHAT_A}, settings_repo=repo
            ).origin
            == "web_session"
        )

    def test_a_session_with_no_chat_id_refuses_rather_than_resolving_nothing(self):
        with pytest.raises(TenantResolutionError, match="carries no chat_id"):
            resolve_tenant_from_web_session({}, settings_repo=FakeSettingsRepo())


class TestTheMissingTenantPolicyIsExplicitAndDistinct:
    """Five call sites disagreed on this; each policy must be reachable."""

    def test_refuse_raises(self):
        with pytest.raises(TenantResolutionError, match=str(UNKNOWN_CHAT)):
            resolve_tenant_from_chat_id(
                UNKNOWN_CHAT, on_missing=REFUSE, settings_repo=FakeSettingsRepo()
            )

    def test_absent_returns_none_without_creating(self):
        repo = FakeSettingsRepo()
        got = resolve_tenant_from_chat_id(
            UNKNOWN_CHAT, on_missing=ABSENT, settings_repo=repo
        )
        assert got.tenant_id is None
        assert repo.created == [], "ABSENT must never write"

    def test_provision_creates_and_says_so_in_the_repo(self):
        repo = FakeSettingsRepo()
        got = resolve_tenant_from_chat_id(
            UNKNOWN_CHAT, on_missing=PROVISION, settings_repo=repo
        )
        assert got.tenant_id == f"created-{UNKNOWN_CHAT}"
        assert repo.created == [UNKNOWN_CHAT]

    def test_refuse_is_the_DEFAULT_so_provisioning_is_never_inherited(self):
        """The consequential one. `dashboard_service` auto-created on any
        unknown chat because `create_if_missing=True` was the default; making
        that the resolver's default would spread it to all five sites."""
        repo = FakeSettingsRepo()
        with pytest.raises(TenantResolutionError):
            resolve_tenant_from_chat_id(UNKNOWN_CHAT, settings_repo=repo)
        assert repo.created == []

    def test_an_unknown_policy_is_refused_not_silently_treated_as_refuse(self):
        with pytest.raises(ValueError, match="on_missing must be one of"):
            resolve_tenant_from_chat_id(
                CHAT_A, on_missing="whatever", settings_repo=FakeSettingsRepo()
            )

    def test_a_missing_inbound_identity_is_never_a_tenant(self):
        with pytest.raises(TenantResolutionError, match="no chat id supplied"):
            resolve_tenant_from_chat_id(
                None, on_missing=ABSENT, settings_repo=FakeSettingsRepo()
            )


class TestItComposesWithTheF1ChokepointRatherThanReplacingIt:
    def test_an_unresolved_tenant_yields_None_and_NOT_system_scope(self):
        """Widening a query on absence is exactly what F.1 retired. If the
        resolver returned SYSTEM_SCOPE here, the chokepoint would wave it
        through and every unresolved request would read across tenants."""
        from src.repositories.tenant_scope import SYSTEM_SCOPE

        got = resolve_tenant_from_chat_id(
            UNKNOWN_CHAT, on_missing=ABSENT, settings_repo=FakeSettingsRepo()
        )
        assert got.scope is None
        assert got.scope is not SYSTEM_SCOPE

    def test_the_chokepoint_refuses_what_the_resolver_could_not_resolve(self):
        """The two halves, exercised together: resolver yields absence, F.1's
        guard is what turns that into a refusal — proving the resolver does not
        need (and does not have) its own enforcement."""
        from src.repositories.tenant_scope import (
            TenantContextError,
            require_tenant_context,
        )

        got = resolve_tenant_from_chat_id(
            UNKNOWN_CHAT, on_missing=ABSENT, settings_repo=FakeSettingsRepo()
        )
        with pytest.raises(TenantContextError):
            require_tenant_context(got.scope, where="test")

    def test_a_resolved_tenant_passes_the_chokepoint(self):
        """Positive control: the refusal above is about absence, not about the
        resolver's return type being unusable."""
        from src.repositories.tenant_scope import require_tenant_context

        got = resolve_tenant_from_chat_id(CHAT_A, settings_repo=FakeSettingsRepo())
        require_tenant_context(got.scope, where="test")
