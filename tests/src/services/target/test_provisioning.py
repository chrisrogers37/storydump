"""The pure parsers in `provisioning` — unit gate, no database.

These exist as a fast tier beside `tests/scripts/test_provisioning_gate.py`,
which is `integration`+`slow` and is skipped wherever Postgres is not
available. What is covered here is only the part that is a pure function of its
argument; every claim about what actually LANDS in a row — idempotency,
tenancy, the seeded cursor, the clock picking it up — lives in the gate,
because none of it can be proven without the schema.

The parsers are not cosmetic. `uq_ig_account_live` and the source's
folder-match are both EXACT string comparisons, so two spellings of one
reference are two destinations posting to one Instagram feed, or two sources
ingesting one Drive folder twice. Normalising is the only place that is
prevented.
"""

from __future__ import annotations

import pytest

from src.services.target.provisioning import (
    ACCOUNT_REF_MAX,
    HANDLE_MAX,
    MANUAL_REF_PREFIX,
    ProvisioningRefused,
    account_ref_from,
    attach_connected_identity,
    folder_ref_from,
    handle_from,
    manual_ref_for,
)


class TestAccountRefFrom:
    def test_a_plain_reference_passes_through(self):
        assert account_ref_from("17841400000000000") == "17841400000000000"

    def test_surrounding_whitespace_is_stripped(self):
        """`"123 "` and `"123"` would be two rows under an exact unique index —
        two schedules against one real feed."""
        assert account_ref_from("  17841400000000000\n") == "17841400000000000"

    @pytest.mark.parametrize(
        "bad", ["", "   ", "\n\t ", None, 17841, 3.5, {"a": 1}, []]
    )
    def test_anything_without_a_value_is_refused_by_name(self, bad):
        with pytest.raises(ProvisioningRefused) as exc:
            account_ref_from(bad)
        assert exc.value.reason == "account_ref_required"

    def test_the_length_cap_refuses_by_its_own_name(self):
        with pytest.raises(ProvisioningRefused) as exc:
            account_ref_from("1" * (ACCOUNT_REF_MAX + 1))
        assert exc.value.reason == "account_ref_too_long"

    def test_the_cap_is_inclusive(self):
        """A boundary asserted in both directions, so the cap cannot drift by
        one without a test noticing."""
        assert account_ref_from("1" * ACCOUNT_REF_MAX) == "1" * ACCOUNT_REF_MAX

    def test_a_non_numeric_reference_is_accepted(self):
        """`provider_account_ref` carries NO format CHECK (054) and the column
        comment calls it opaque. Rejecting a shape Meta has not shipped yet
        would be this module inventing a constraint the schema declined to
        make."""
        assert account_ref_from("ig_abc-123") == "ig_abc-123"


class TestFolderRefFrom:
    def test_a_bare_id_passes_through(self):
        assert folder_ref_from("1AbCdEfGhIjK") == "1AbCdEfGhIjK"

    @pytest.mark.parametrize(
        "pasted",
        [
            "https://drive.google.com/drive/folders/1AbCdEfGhIjK",
            "https://drive.google.com/drive/folders/1AbCdEfGhIjK?usp=sharing",
            "https://drive.google.com/drive/folders/1AbCdEfGhIjK/",
            "https://drive.google.com/drive/folders/1AbCdEfGhIjK#anchor",
            "  https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjK  ",
        ],
    )
    def test_every_shape_of_pasted_url_yields_the_bare_id(self, pasted):
        """ "Paste the folder" means the address bar to everyone who has not
        read the Drive API docs. Storing the URL would fail much later, at the
        first list call, as something that reads like a Drive fault."""
        assert folder_ref_from(pasted) == "1AbCdEfGhIjK"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            None,
            12,
            [],
            "https://drive.google.com/drive/folders/",
            "https://drive.google.com/drive/folders/?usp=sharing",
        ],
    )
    def test_anything_without_a_folder_is_refused_by_name(self, bad):
        with pytest.raises(ProvisioningRefused) as exc:
            folder_ref_from(bad)
        assert exc.value.reason == "folder_required"

    @pytest.mark.parametrize(
        "markerless",
        [
            "https://example.com/x",
            "https://drive.google.com/open?id=ABC123",
            "drive.google.com/folderview?id=ABC123",
            "https://drive.google.com/",
            "https://drive.google.com",
        ],
    )
    def test_a_url_without_the_marker_is_refused_not_salvaged(self, markerless):
        """A link with no `/folders/` marker has no id to extract.

        The delimiter cut runs regardless of whether the marker was found, so
        these used to chew down to `https:` / `drive.google.com` and be RETURNED
        as folder ids. An earlier version of this test asserted exactly that,
        which turned the defect into the specification.
        """
        with pytest.raises(ProvisioningRefused) as exc:
            folder_ref_from(markerless)
        assert exc.value.reason == "folder_not_a_drive_folder"

    def test_two_different_markerless_urls_cannot_collide_on_one_ref(self):
        """The bite was collision, not just a nonsense id.

        Every markerless URL reduced to the same handful of tokens, so two
        UNRELATED folders produced one ref, the idempotency key matched, and the
        second paste silently returned the FIRST source with ``created=False``.
        One person, two links, one source, no signal.

        Asserted as a property over the pairs rather than as two raises: what
        must never come back is a shared value, so the test names sameness.
        """
        a = "https://drive.google.com/open?id=AAA"
        b = "https://drive.google.com/open?id=BBB"

        refs = []
        for url in (a, b):
            try:
                refs.append(folder_ref_from(url))
            except ProvisioningRefused:
                pass  # refused is the correct outcome; it yields no ref to collide

        assert refs == [], (
            "distinct folder links produced storable refs "
            f"{refs!r} — if these are ever equal, two folders become one source"
        )

    def test_a_bare_id_survives_the_url_shape_guard(self):
        """The positive control on the guard.

        A denylist that refused too much would make the whole writer unusable,
        and every other passing test here would still pass — they run through
        `/folders/` URLs, which strip to the same id.
        """
        assert folder_ref_from("1AbCdEfGhIjK-_9") == "1AbCdEfGhIjK-_9"


class TestTheRefusalCarriesItsReason:
    def test_reason_is_an_attribute_not_only_a_message(self):
        """`app.py` maps on `.reason`; a refusal that only carried prose would
        fall through to the unmapped 500 handler."""
        exc = ProvisioningRefused("folder_required", "detail here")
        assert exc.reason == "folder_required"
        assert "folder_required" in str(exc)
        assert "detail here" in str(exc)


class TestHandleFrom:
    """The typed-handle path (#1089). Same rule as `account_ref_from`: this is
    the ONLY place a spelling is normalised, and `uq_ig_account_live` is an
    exact comparison downstream."""

    def test_a_plain_handle_passes_through(self):
        assert handle_from("thehandle") == "thehandle"

    def test_the_at_sign_people_type_is_dropped(self):
        assert handle_from("@thehandle") == "thehandle"

    def test_surrounding_whitespace_is_trimmed(self):
        assert handle_from("  @thehandle\n") == "thehandle"

    @pytest.mark.parametrize("bad", ["", "   ", "@", " @ ", None, 17841, {"a": 1}])
    def test_an_absent_handle_is_refused_by_name(self, bad):
        with pytest.raises(ProvisioningRefused) as exc:
            handle_from(bad)
        assert exc.value.reason == "handle_required"

    def test_interior_whitespace_is_a_refusal_not_a_repair(self):
        """`"two words"` is not a handle with a typo in it. Stripping the space
        would create a destination for an account nobody named."""
        with pytest.raises(ProvisioningRefused) as exc:
            handle_from("two words")
        assert exc.value.reason == "handle_malformed"

    def test_a_handle_longer_than_the_column_is_refused_by_name(self):
        """`ig_accounts.handle` is VARCHAR(50). Past it, Postgres would answer
        with a 500 rather than a sentence naming the field."""
        with pytest.raises(ProvisioningRefused) as exc:
            handle_from("a" * (HANDLE_MAX + 1))
        assert exc.value.reason == "handle_too_long"

    def test_exactly_the_column_width_is_accepted(self):
        assert handle_from("a" * HANDLE_MAX) == "a" * HANDLE_MAX

    def test_instagram_own_character_rules_are_not_asserted(self):
        """A provider rule this tier cannot verify. Guessing it refuses a handle
        the provider accepts — the same reasoning `ACCOUNT_REF_MAX` records."""
        assert handle_from("a.handle_1") == "a.handle_1"


class TestManualRefFor:
    def test_the_reference_is_namespaced(self):
        """Bare, it would collide the day OAuth supplies the real Meta id: the
        same feed under two references, two destinations, two schedules."""
        assert manual_ref_for("thehandle") == f"{MANUAL_REF_PREFIX}thehandle"

    def test_two_spellings_of_one_handle_produce_ONE_reference(self):
        """The idempotency half. `uq_ig_account_live` compares bytes, so without
        folding `@Foo` and `@foo` are two destinations against one real feed."""
        assert manual_ref_for(handle_from("@Foo")) == manual_ref_for(handle_from("foo"))

    def test_a_derived_reference_fits_the_reference_column(self):
        assert len(manual_ref_for("a" * HANDLE_MAX)) <= ACCOUNT_REF_MAX


class _AttachExecutor:
    """Scripts the UPDATE's rowcount and the classifying SELECT's row."""

    def __init__(self, rowcount, current=None):
        self.rowcount, self.current, self.statements = rowcount, current, []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        executor = self

        class _R:
            rowcount = executor.rowcount

            def mappings(self_inner):
                return self_inner

            def first(self_inner):
                return executor.current

        return _R()


class TestAttachConnectedIdentityRefusesTheWrongAccount:
    """A destination is for ONE Instagram account (#1221 review, `07` §2).
    The UPDATE's WHERE carries the rule; these pin the WHERE's shape and the
    named refusals when nothing moved. The rule against a real database is
    the gate's business."""

    async def test_the_update_accepts_only_the_same_real_id_or_the_matching_manual_handle(
        self,
    ):
        ex = _AttachExecutor(rowcount=1)
        await attach_connected_identity(
            ex,
            workspace_id="ws",
            ig_account_id="acct",
            provider_account_ref="17841400000000001",
            handle="GatorTails",
        )
        ((sql, params),) = ex.statements
        assert "provider_account_ref = :ref" in sql
        assert "provider_account_ref LIKE :manual_prefix" in sql
        assert "provider_account_ref = :expected_manual" in sql
        assert "w.state = 'active'" in sql
        assert params["expected_manual"] == "manual:gatortails"
        assert params["manual_prefix"] == "manual:%"

    async def test_a_profile_without_a_username_cannot_check_the_handle_and_is_allowed(
        self,
    ):
        ex = _AttachExecutor(rowcount=1)
        await attach_connected_identity(
            ex,
            workspace_id="ws",
            ig_account_id="acct",
            provider_account_ref="1784",
            handle=None,
        )
        ((_, params),) = ex.statements
        assert params["expected_manual"] is None and params["handle"] is None

    async def test_a_row_on_a_different_real_account_is_refused_as_wrong_account(self):
        ex = _AttachExecutor(
            rowcount=0,
            current={
                "provider_account_ref": "999",
                "state": "active",
                "workspace_state": "active",
            },
        )
        with pytest.raises(ProvisioningRefused) as info:
            await attach_connected_identity(
                ex,
                workspace_id="ws",
                ig_account_id="acct",
                provider_account_ref="1784",
                handle="x",
            )
        assert info.value.reason == "wrong_account"

    async def test_a_typed_handle_that_is_not_the_signed_in_account_is_refused(self):
        ex = _AttachExecutor(
            rowcount=0,
            current={
                "provider_account_ref": "manual:foo",
                "state": "active",
                "workspace_state": "active",
            },
        )
        with pytest.raises(ProvisioningRefused) as info:
            await attach_connected_identity(
                ex,
                workspace_id="ws",
                ig_account_id="acct",
                provider_account_ref="1784",
                handle="bar",
            )
        assert info.value.reason == "wrong_account"

    @pytest.mark.parametrize(
        "current",
        [
            None,
            {
                "provider_account_ref": "manual:foo",
                "state": "moved",
                "workspace_state": "active",
            },
            {
                "provider_account_ref": "manual:foo",
                "state": "active",
                "workspace_state": "offboarding",
            },
        ],
    )
    async def test_a_missing_moved_or_offboarding_destination_is_not_found(
        self, current
    ):
        ex = _AttachExecutor(rowcount=0, current=current)
        with pytest.raises(ProvisioningRefused) as info:
            await attach_connected_identity(
                ex,
                workspace_id="ws",
                ig_account_id="acct",
                provider_account_ref="1784",
                handle="foo",
            )
        assert info.value.reason == "not_found"


class TestConnectDestinationAdoptsOrCreates:
    """A workspace-level connect pins NO destination (owner ruling 2026-09-04:
    destinations are added by connecting). The identity Instagram returns
    lands on the ONE row the account already has here — its real id, or the
    typed handle that named it — and creates the destination only when
    neither exists. Adoption goes through `attach_connected_identity`, so the
    same-account rule and the reauth flip are the same code path as a
    per-row connect."""

    @pytest.fixture
    def seams(self, monkeypatch):
        from src.services.target import provisioning

        log = []
        found = {"row": None, "sql": None}

        async def row(executor, sql, **params):
            found["sql"] = sql
            log.append(("find", params))
            return found["row"]

        async def attach(
            executor, *, workspace_id, ig_account_id, provider_account_ref, handle
        ):
            log.append(
                ("attach", workspace_id, ig_account_id, provider_account_ref, handle)
            )

        async def create(
            executor,
            *,
            workspace_id,
            provider_account_ref=None,
            handle=None,
            schedule=True,
        ):
            log.append(("create", workspace_id, provider_account_ref, handle, schedule))
            return ("new-id", True)

        monkeypatch.setattr(provisioning.readers, "row", row)
        monkeypatch.setattr(provisioning, "attach_connected_identity", attach)
        monkeypatch.setattr(provisioning, "create_destination", create)
        return log, found

    async def test_an_existing_row_for_this_account_is_adopted_not_duplicated(
        self, seams
    ):
        from src.services.target.provisioning import connect_destination

        log, found = seams
        found["row"] = {"workspace_state": "active", "id": "acct-1"}
        result = await connect_destination(
            object(),
            workspace_id="ws",
            provider_account_ref="1784",
            handle="GatorTails",
        )
        assert result == ("acct-1", False)
        assert ("attach", "ws", "acct-1", "1784", "GatorTails") in log
        assert not any(entry[0] == "create" for entry in log)

    async def test_the_lookup_matches_the_real_id_or_the_folded_typed_handle(
        self, seams
    ):
        from src.services.target.provisioning import connect_destination

        log, found = seams
        found["row"] = {"workspace_state": "active", "id": "acct-1"}
        await connect_destination(
            object(),
            workspace_id="ws",
            provider_account_ref="1784",
            handle="GatorTails",
        )
        params = log[0][1]
        assert params["ws"] == "ws"
        assert params["ref"] == "1784"
        assert params["manual_ref"] == "manual:gatortails"
        assert "state <> 'moved'" in found["sql"]
        assert "FROM workspaces w" in found["sql"], (
            "the workspace's state rides the same read"
        )

    async def test_without_a_username_only_the_real_id_can_match(self, seams):
        from src.services.target.provisioning import connect_destination

        log, found = seams
        found["row"] = {"workspace_state": "active", "id": None}
        await connect_destination(
            object(), workspace_id="ws", provider_account_ref="1784", handle=None
        )
        assert log[0][1]["manual_ref"] is None

    async def test_a_pinned_destination_skips_the_lookup_and_attaches_to_it(
        self, seams
    ):
        from src.services.target.provisioning import connect_destination

        log, found = seams
        result = await connect_destination(
            object(),
            workspace_id="ws",
            provider_account_ref="1784",
            handle="GatorTails",
            ig_account_id="pinned",
        )
        assert result == ("pinned", False)
        assert log == [("attach", "ws", "pinned", "1784", "GatorTails")]

    async def test_no_row_creates_a_scheduled_destination_from_the_grant(self, seams):
        from src.services.target.provisioning import connect_destination

        log, found = seams
        found["row"] = {"workspace_state": "active", "id": None}
        result = await connect_destination(
            object(),
            workspace_id="ws",
            provider_account_ref="1784",
            handle="GatorTails",
        )
        assert result == ("new-id", True)
        assert ("create", "ws", "1784", "GatorTails", True) in log
        assert not any(entry[0] == "attach" for entry in log)

    async def test_a_workspace_that_is_not_active_refuses_the_add_by_name(self, seams):
        from src.services.target.provisioning import connect_destination

        log, found = seams
        found["row"] = {"workspace_state": "offboarding", "id": None}
        with pytest.raises(ProvisioningRefused) as info:
            await connect_destination(
                object(), workspace_id="ws", provider_account_ref="1784", handle="x"
            )
        assert info.value.reason == "workspace_inactive"
        assert [entry[0] for entry in log] == ["find"]

    async def test_a_workspace_that_does_not_exist_is_not_found(self, seams):
        from src.services.target.provisioning import connect_destination

        log, found = seams
        found["row"] = None
        with pytest.raises(ProvisioningRefused) as info:
            await connect_destination(
                object(), workspace_id="ws", provider_account_ref="1784", handle="x"
            )
        assert info.value.reason == "not_found"

    async def test_a_username_the_local_rule_refuses_does_not_veto_the_connect(
        self, seams
    ):
        """`_identity_for`'s lesson, applied: a display value cannot refuse an
        identity-bearing write. The real id lands; the username is dropped."""
        from src.services.target.provisioning import connect_destination

        log, found = seams
        result = await connect_destination(
            object(),
            workspace_id="ws",
            provider_account_ref="1784",
            handle="two words",
            ig_account_id="pinned",
        )
        assert result == ("pinned", False)
        assert log == [("attach", "ws", "pinned", "1784", None)]


class _ScriptedExecutor:
    """Answers each `execute` from a queue of (rowcount, first_row) pairs."""

    def __init__(self, *results):
        self.results, self.statements = list(results), []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        rowcount, first = self.results.pop(0)

        class _R:
            def mappings(self_inner):
                return self_inner

            def first(self_inner):
                return first

        _R.rowcount = rowcount
        return _R()


class TestDisableDestination:
    """Remove, in the port's terms (owner decision 2026-09-04): the
    destination leaves the clock's scan (`state = 'disabled'`), its Instagram
    credential is revoked locally, and its live intents are flagged for
    cancellation the way `cancel` flags one — the worker finishes them on the
    next touch (`06` "account disabled"). The row stays: history, and the
    connect that brings it back, both need it."""

    async def test_disables_revokes_and_flags_in_that_order(self):
        from src.services.target.provisioning import disable_destination

        ex = _ScriptedExecutor((1, {"id": "acct"}), (1, None), (2, None))
        result = await disable_destination(ex, workspace_id="ws", ig_account_id="acct")
        assert result == {"credential_revoked": True, "intents_flagged": 2}
        (disable, revoke, flag) = ex.statements
        assert "UPDATE ig_accounts" in disable[0]
        assert "state = 'disabled'" in disable[0]
        assert "state IN ('active', 'reauth_required')" in disable[0]
        assert disable[1] == {"acct": "acct", "ws": "ws"}
        assert "UPDATE oauth_credentials SET state = 'revoked'" in revoke[0]
        assert revoke[1]["provider"] == "ig_login"
        assert "UPDATE post_intents SET cancel_requested = true" in flag[0]
        assert "NOT cancel_requested" in flag[0]
        assert flag[1]["acct"] == "acct" and flag[1]["ws"] == "ws"

    async def test_nothing_to_revoke_or_flag_is_reported_not_invented(self):
        from src.services.target.provisioning import disable_destination

        ex = _ScriptedExecutor((1, {"id": "acct"}), (0, None), (0, None))
        result = await disable_destination(ex, workspace_id="ws", ig_account_id="acct")
        assert result == {"credential_revoked": False, "intents_flagged": 0}

    async def test_an_unknown_or_moved_destination_is_not_found(self):
        from src.services.target.provisioning import disable_destination

        for current in (None, {"state": "moved"}):
            ex = _ScriptedExecutor((0, None), (0, current))
            with pytest.raises(ProvisioningRefused) as info:
                await disable_destination(ex, workspace_id="ws", ig_account_id="acct")
            assert info.value.reason == "not_found"
            assert len(ex.statements) == 2, "nothing is revoked or flagged"

    async def test_disabling_twice_is_refused_by_name(self):
        from src.services.target.provisioning import disable_destination

        ex = _ScriptedExecutor((0, None), (0, {"state": "disabled"}))
        with pytest.raises(ProvisioningRefused) as info:
            await disable_destination(ex, workspace_id="ws", ig_account_id="acct")
        assert info.value.reason == "already_disabled"
