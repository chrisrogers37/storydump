"""#900 / #898 / #901 — the read/write-asymmetry hardening sweep.

**The pattern that ties all three, and it shaped the fixes rather than only the
tests:** every one leaks through a path that *precedes or accompanies a
correctly-enforced refusal*. The enforcement was never missing — it simply was
not the only thing that spoke. That is why reviewing the mutation path alone
reads clean, and why the original was found by clicking a control the list had
offered.

So each fix here silences the *other* speaker rather than adding a second
refusal:

* **#900** — `POST /add-account` resolved an account by `meta_account_id`
  DEPLOYMENT-WIDE and then wrote to it. The write is the severity outlier of
  the sweep: everything else discloses, this one modifies another tenant's row.
  Fixed by routing through `_require_account_ownership`, the same gate
  Switch/Remove already enforce — and deliberately answering with the SAME
  generic 400 as bad credentials, so the new gate does not become the oracle
  it closes.
* **#898** — `/system-status` returned deployment-wide health to any
  authenticated tenant, including another tenant's media category and count.
  Its sibling `/analytics/service-health` in the same file was already gated;
  this one was missed. Note the leak got WORSE today for a benign reason: #906
  fixed the health check so it stops raising and starts returning data, and
  this endpoint is an ungated consumer of exactly that data.
* **#901** — the id-prefix lookup, the confirm dialog it feeds, the error text,
  and the deployment-wide count. All disclosure ahead of a working refusal.

Two tenants at the door in every case, and each test is born-red: the
mutation battery in the PR body records which fix each one names.
"""

from unittest.mock import Mock, patch

import pytest

from tests.src.api.conftest import CHAT_ID, mock_validate, service_ctx

#: The attacker: a second, authenticated tenant. Authentication is not the
#: gap in any of these — every caller here is a legitimate, logged-in user.
OTHER_CHAT_ID = -1009876543210

#: A meta account id belonging to the FIRST tenant.
VICTIM_META_ID = "17841400000000001"


@pytest.mark.unit
class TestAddAccountRefusesACrossTenantWrite:
    """#900 — the only WRITE in the sweep."""

    def _post(self, client, chat_id):
        return client.post(
            "/api/onboarding/add-account",
            json={
                "init_data": "fake",
                "chat_id": chat_id,
                "display_name": "Attacker Label",
                "instagram_account_id": VICTIM_META_ID,
                "access_token": "valid-token-for-the-victims-account",
            },
        )

    def _instagram_api_says_yes(self):
        """Credential possession is MITIGATED, tenancy is not — so the test
        must grant the attacker valid credentials. A test where the Graph API
        rejects the token would pass without ever reaching the gap."""
        response = Mock()
        response.status_code = 200
        response.json = Mock(return_value={"username": "victim_handle"})
        return response

    def test_a_second_tenant_cannot_re_stamp_the_first_tenants_account(self, client):
        from src.api.routes.onboarding import settings as settings_module

        with (
            mock_validate(),
            patch.object(settings_module, "InstagramAccountService") as svc_cls,
            patch("httpx.AsyncClient") as http_cls,
        ):
            http_client = http_cls.return_value.__aenter__.return_value
            http_client.get = Mock(return_value=self._instagram_api_says_yes())

            # awaited in the route
            async def _get(*a, **k):
                return self._instagram_api_says_yes()

            http_client.get = _get

            svc = service_ctx(svc_cls)
            existing = Mock(id="acct-owned-by-tenant-A")
            svc.get_account_by_meta_id = Mock(return_value=existing)
            # The gate refuses exactly as it does for Switch/Remove.
            svc._require_account_ownership = Mock(
                side_effect=ValueError("Account acct-owned-by-tenant-A not found")
            )
            svc.update_account_token = Mock()
            svc.add_account = Mock()

            resp = self._post(client, OTHER_CHAT_ID)

        assert resp.status_code == 400, (
            f"a foreign tenant's add-account returned {resp.status_code} — the"
            " cross-tenant write was not refused (#900)"
        )
        svc.update_account_token.assert_not_called()
        svc.add_account.assert_not_called()

    def test_the_refusal_is_not_an_existence_oracle(self, client):
        """The refusal must be indistinguishable from bad credentials.

        A distinct message would replace a write vulnerability with the
        enumeration one the same sweep is closing — #891's shape.
        """
        from src.api.routes.onboarding import settings as settings_module

        async def _ok(*a, **k):
            return self._instagram_api_says_yes()

        bodies = {}
        for label, refuse in (("foreign", True), ("bad-credentials", False)):
            with (
                mock_validate(),
                patch.object(settings_module, "InstagramAccountService") as svc_cls,
                patch("httpx.AsyncClient") as http_cls,
            ):
                http_client = http_cls.return_value.__aenter__.return_value
                if refuse:
                    http_client.get = _ok
                    svc = service_ctx(svc_cls)
                    svc.get_account_by_meta_id = Mock(return_value=Mock(id="a"))
                    svc._require_account_ownership = Mock(
                        side_effect=ValueError("not found")
                    )
                else:

                    async def _rejected(*a, **k):
                        response = Mock()
                        response.status_code = 400
                        response.json = Mock(
                            return_value={"error": {"message": "Invalid OAuth token"}}
                        )
                        return response

                    http_client.get = _rejected
                    service_ctx(svc_cls)
                resp = self._post(client, OTHER_CHAT_ID)
                bodies[label] = (resp.status_code, resp.json().get("detail"))

        assert bodies["foreign"] == bodies["bad-credentials"], (
            f"the two refusals are distinguishable: {bodies} — a caller can"
            " now tell 'that account exists and is not yours' from 'those"
            " credentials are wrong', which is the oracle this gate closes"
        )


@pytest.mark.unit
class TestSystemStatusIsAdminOnly:
    """#898 — deployment-wide telemetry, one tap from the home screen."""

    def _get(self, client, chat_id=CHAT_ID):
        return client.get(
            f"/api/onboarding/system-status?init_data=fake&chat_id={chat_id}"
        )

    def test_an_authenticated_NON_admin_tenant_is_refused(self, client):
        from src.api.routes.onboarding import helpers as helpers_module

        with (
            mock_validate(),
            patch.object(helpers_module, "MembershipService") as ms_cls,
        ):
            ms = service_ctx(ms_cls)
            ms.is_system_admin = Mock(return_value=False)
            resp = self._get(client)

        assert resp.status_code == 403, (
            f"an authenticated non-admin got {resp.status_code} from"
            " /system-status — authentication proves who the caller is, it"
            " does not make them an operator (#898)"
        )

    def test_an_administrator_still_gets_the_checks(self, client):
        """Positive control. Without it, an endpoint that 403'd everyone —
        or 500'd — would satisfy the refusal test above."""
        from src.api.routes.onboarding import dashboard as dashboard_module
        from src.api.routes.onboarding import helpers as helpers_module

        with (
            mock_validate(),
            patch.object(helpers_module, "MembershipService") as ms_cls,
            patch.object(dashboard_module, "HealthCheckService") as hc_cls,
        ):
            ms = service_ctx(ms_cls)
            ms.is_system_admin = Mock(return_value=True)
            hc = service_ctx(hc_cls)
            hc.check_all = Mock(return_value={"status": "healthy", "checks": {}})
            resp = self._get(client)

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "healthy"

    def test_it_is_gated_the_same_way_as_its_already_gated_sibling(self):
        """The intent, asserted structurally rather than by reading.

        `/analytics/service-health` was gated for exactly this reason and this
        endpoint was missed. Pinning that they now use the SAME gate is what
        stops the next endpoint in this file drifting apart from it again.
        """
        import inspect

        from src.api.routes.onboarding import dashboard

        for name in ("onboarding_system_status", "onboarding_service_health"):
            source = inspect.getsource(getattr(dashboard, name))
            assert "_validate_admin(" in source, (
                f"{name} does not call _validate_admin — deployment-wide"
                " telemetry behind a tenant-level gate (#898)"
            )
            assert "_validate_request(" not in source, (
                f"{name} still calls _validate_request; a tenant gate on a"
                " fleet-wide view is the gap, not the fix"
            )


@pytest.mark.unit
class TestTheLookupsThatPrecedeARefusalAreScoped:
    """#901 — disclosure ahead of, or alongside, a working refusal."""

    def _service(self):
        from src.services.core.instagram_account_service import (
            InstagramAccountService,
        )

        service = InstagramAccountService.__new__(InstagramAccountService)
        return service

    def test_the_prefix_lookup_resolves_only_within_the_callers_tenant(self):
        """8 hex characters used to resolve ANY tenant's account, ahead of a
        mutation door that would correctly refuse — an enumeration oracle, and
        the reason the confirm dialog could render a foreign name."""
        service = self._service()
        mine = Mock(id="aaaaaaaa-1111-1111-1111-111111111111")
        theirs = Mock(id="bbbbbbbb-2222-2222-2222-222222222222")

        service.settings_repo = Mock()
        service.settings_repo.get_by_chat_id = Mock(return_value=Mock(id="cs-mine"))
        service.list_accounts = Mock(return_value=[mine])

        assert service.get_account_by_id_prefix("aaaaaaaa", CHAT_ID) is mine, (
            "positive control: the caller's OWN account must still resolve"
        )
        assert service.get_account_by_id_prefix("bbbbbbbb", CHAT_ID) is None, (
            "a foreign account resolved from its 8-hex prefix — the caller can"
            " enumerate what exists before being refused, and whatever renders"
            " the result discloses its name and handle (#901)"
        )
        assert service.get_account_by_id_prefix("cccccccc", CHAT_ID) is None, (
            "an INVENTED prefix must answer the same as a foreign one, or the"
            " difference is itself the oracle"
        )
        assert theirs is not None  # kept explicit: the row exists, unreachable

    def test_an_unknown_chat_gets_the_same_answer_as_a_non_owner(self):
        service = self._service()
        service.settings_repo = Mock()
        service.settings_repo.get_by_chat_id = Mock(return_value=None)
        assert service.get_account_by_id_prefix("aaaaaaaa", 12345) is None

    def test_the_already_exists_error_does_not_name_a_foreign_account(self):
        """Error text is a disclosure surface and is routinely exempted from
        review attention — which is exactly why it carried a foreign name."""
        service = self._service()
        foreign = Mock(display_name="Victim Brand Account")
        service.get_account_by_meta_id = Mock(return_value=foreign)
        service.get_account_by_username = Mock(return_value=None)

        with pytest.raises(ValueError) as exc:
            service._validate_new_account(VICTIM_META_ID, "someone_else")

        message = str(exc.value)
        assert "Victim Brand Account" not in message, (
            f"the error names a foreign account: {message!r} (#901)"
        )
        assert VICTIM_META_ID in message, (
            "the id the CALLER supplied should still be echoed — removing that"
            " too would make the message useless without making it safer"
        )

    def test_the_active_account_count_is_the_tenants_not_the_deployments(self):
        """A deployment-wide count shaped tenant UI, so a tenant with one
        account could infer that others existed."""
        service = self._service()
        service.settings_repo = Mock()
        service.settings_repo.get_by_chat_id = Mock(return_value=Mock(id="cs-mine"))
        service.list_accounts = Mock(return_value=[Mock(), Mock()])
        service.account_repo = Mock()
        service.account_repo.count_active = Mock(return_value=97)

        assert service.count_active_accounts(CHAT_ID) == 2, (
            "the count is deployment-wide — it must answer the same set the"
            " caller's own list shows (#901)"
        )
        service.account_repo.count_active.assert_not_called()

    def test_an_unknown_chat_counts_zero_rather_than_the_deployment(self):
        service = self._service()
        service.settings_repo = Mock()
        service.settings_repo.get_by_chat_id = Mock(return_value=None)
        service.account_repo = Mock()
        service.account_repo.count_active = Mock(return_value=97)
        assert service.count_active_accounts(999) == 0
        service.account_repo.count_active.assert_not_called()
