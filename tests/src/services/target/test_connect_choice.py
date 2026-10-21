"""The history choice an Instagram connect carries (#1644) — the wire.

What is pinned here, without a database: the start body's shape and every
refusal it names; that nothing is read, and nothing recorded, where the
past-posts source is not live; what a start records for each entry; the preview's
fields and its equal-reset arithmetic; and what the callback hands
`connect_past_posts` and writes as the result. The SQL itself, the CHECKs and the
real composition are `tests/scripts/test_oauth_past_posts_choice_gate.py`'s.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest

from src.services.target import (
    connect_choice,
    source_policy,
    source_state,
    versioned_mix,
)
from src.services.target.connect_choice import ConnectChoiceRefused
from src.services.target.instagram_sources import (
    PastPostsAuthorization,
    PastPostsCapability,
)
from src.services.target.source_membership import PastPostsChoice, MembershipResult
from src.services.target.versioned_mix import MixConflict

WS = "33333333-3333-4333-8333-333333333333"
ACCOUNT = "55555555-5555-4555-8555-555555555555"
LIVE = PastPostsCapability(enabled=True)


def body(use_history=True, revision=8, version=1, **extra):
    return {
        "past_posts_choice": {
            "use_history": use_history,
            "expected_mix_revision": revision,
            "preview_version": version,
            **extra,
        }
    }


class _RefusesSQL:
    """An executor that must never be reached."""

    async def execute(self, *args, **kwargs):
        raise AssertionError("reached SQL where the past-posts source is not live")


class _Scripted:
    """Answers each `execute` with the next scripted first row, recording the
    statement and its parameters."""

    def __init__(self, *rows):
        self.rows, self.statements = list(rows), []

    async def execute(self, statement, params=None):
        self.statements.append((" ".join(str(statement).split()), params))
        first = self.rows.pop(0)

        class _R:
            def mappings(self_inner):
                return self_inner

            def first(self_inner):
                return first

        return _R()


class TestParseChoice:
    @pytest.mark.parametrize("value", [{}, {"past_posts_choice": None}])
    def test_no_choice_parses_to_none(self, value):
        assert connect_choice.parse_choice(value) is None

    def test_a_choice_carries_its_three_facts(self):
        assert connect_choice.parse_choice(body()) == PastPostsChoice(True, 8, 1)
        assert connect_choice.parse_choice(body(False, 0)) == PastPostsChoice(
            False, 0, 1
        )

    def test_the_revision_is_bounded_by_its_bigint_column(self):
        assert (
            connect_choice.parse_choice(body(revision=2**63 - 1)).expected_mix_revision
            == 2**63 - 1
        )

    @pytest.mark.parametrize(
        "value",
        [
            [],
            "past_posts_choice",
            {"past_posts_choice": None, "purpose": "connect"},
            {"past_posts_choice": []},
            {"past_posts_choice": "on"},
            body(extra=True),
            {"past_posts_choice": {"use_history": True, "expected_mix_revision": 8}},
            body(use_history="true"),
            body(use_history=1),
            body(revision="8"),
            body(revision=8.0),
            body(revision=True),
            body(revision=-1),
            body(revision=2**63),
            body(version=2),
            body(version=True),
            body(version="1"),
        ],
        ids=[
            "list",
            "string",
            "unknown-top-level-key",
            "choice-a-list",
            "choice-a-string",
            "unknown-choice-key",
            "missing-preview-version",
            "use-history-a-string",
            "use-history-an-int",
            "revision-a-string",
            "revision-a-float",
            "revision-a-bool",
            "revision-negative",
            "revision-past-bigint",
            "another-preview-version",
            "preview-version-a-bool",
            "preview-version-a-string",
        ],
    )
    def test_anything_else_is_refused_by_name(self, value):
        with pytest.raises(ConnectChoiceRefused) as refused:
            connect_choice.parse_choice(value)
        assert refused.value.reason == "invalid_past_posts_choice"

    def test_an_unknown_reason_cannot_be_raised(self):
        with pytest.raises(ValueError):
            ConnectChoiceRefused("mix_conflict")


class TestLiveRevision:
    @pytest.fixture
    def seams(self, monkeypatch):
        log = {"calls": [], "saved": None}

        async def lock(executor, *, workspace_id, **kw):
            log["calls"].append(("lock", workspace_id, kw))

        async def state(executor, ws):
            log["calls"].append(("state", ws))
            return log["saved"]

        monkeypatch.setattr(source_state, "lock_workspace_sources", lock)
        monkeypatch.setattr(versioned_mix, "state", state)
        return log

    @pytest.mark.parametrize("capability", [None, PastPostsCapability(enabled=False)])
    async def test_without_an_enabled_capability_nothing_is_read(
        self, seams, capability
    ):
        assert (
            await connect_choice.live_revision(
                _RefusesSQL(), workspace_id=WS, capability=capability
            )
            is None
        )
        assert seams["calls"] == []

    async def test_a_converted_mix_answers_its_revision_under_the_prefix(self, seams):
        seams["saved"] = {
            "converted": True,
            "source_mix_policy_version": 1,
            "source_mix_conversion_version": 1,
            "source_mix_revision": 8,
        }
        revision = await connect_choice.live_revision(
            object(), workspace_id=WS, capability=LIVE
        )
        assert revision == 8
        assert seams["calls"] == [("lock", WS, {}), ("state", WS)]

    async def test_an_unconverted_mix_is_not_live(self, seams):
        seams["saved"] = {
            "converted": False,
            "source_mix_policy_version": None,
            "source_mix_conversion_version": None,
            "source_mix_revision": None,
        }
        assert (
            await connect_choice.live_revision(
                object(), workspace_id=WS, capability=LIVE
            )
            is None
        )


@pytest.fixture
def world(monkeypatch):
    """`live_revision` and the named account's facts behind seams."""
    log = {"revision": 8, "account": None, "asked": []}

    async def live_revision(executor, *, workspace_id, capability):
        log["asked"].append(("live", workspace_id, capability))
        return log["revision"] if capability is LIVE else None

    async def account(executor, ws, account_id):
        log["asked"].append(("account", ws, account_id))
        return log["account"]

    monkeypatch.setattr(connect_choice, "live_revision", live_revision)
    monkeypatch.setattr(connect_choice, "_account", account)
    return log


def facts(*, handle="example_handle", has_past_posts=False, history_enabled=False):
    return {
        "handle": handle,
        "has_past_posts": has_past_posts,
        "history_enabled": history_enabled,
    }


class TestStartFields:
    async def start(
        self, value, *, capability=LIVE, account_id=None, purpose="connect"
    ):
        """A start as the route makes it: the body *value* parsed first, with no
        session and nothing read, then the choice handed to the service."""
        return await connect_choice.start_fields(
            object(),
            workspace_id=WS,
            choice=connect_choice.parse_choice(value),
            account_id=account_id,
            purpose=purpose,
            capability=capability,
        )

    async def test_no_choice_where_the_past_posts_is_not_live_records_nothing(
        self, world
    ):
        assert await self.start({}, capability=None) == {}

    @pytest.mark.parametrize(
        "revision", [8, None], ids=["no-capability", "unconverted"]
    )
    async def test_a_choice_where_the_past_posts_is_not_live_is_refused(
        self, world, revision
    ):
        world["revision"] = revision
        with pytest.raises(ConnectChoiceRefused) as refused:
            await self.start(body(), capability=None if revision else LIVE)
        assert refused.value.reason == "past_posts_not_activated"

    async def test_a_shape_that_is_not_the_contracts_never_reaches_the_service(
        self, world
    ):
        """The shape is `parse_choice`'s, judged by the route with the body in
        hand and before its session opens (the gate proves the connection)."""
        with pytest.raises(ConnectChoiceRefused) as refused:
            await self.start(body(version=2))
        assert refused.value.reason == "invalid_past_posts_choice"
        assert world["asked"] == []

    async def test_the_service_takes_the_parsed_choice_and_never_a_body(self, world):
        with pytest.raises(TypeError):
            await connect_choice.start_fields(
                object(), workspace_id=WS, body=body(), capability=LIVE
            )

    async def test_add_records_the_entry_and_the_choice(self, world):
        assert await self.start(body(True, 8)) == {
            "past_posts_entry": "add",
            "past_posts_use_history": True,
            "past_posts_expected_mix_revision": 8,
            "past_posts_preview_version": 1,
        }
        assert ("account", WS, None) not in world["asked"]

    async def test_add_without_a_choice_records_the_entry_alone(self, world):
        assert await self.start({"past_posts_choice": None}) == {
            "past_posts_entry": "add"
        }

    async def test_on_against_a_moved_revision_is_a_mix_conflict(self, world):
        world["revision"] = 9
        with pytest.raises(MixConflict) as conflict:
            await self.start(body(True, 8))
        assert conflict.value.current_revision == 9

    async def test_off_is_never_stale(self, world):
        world["revision"] = 9
        fields = await self.start(body(False, 8))
        assert fields["past_posts_use_history"] is False
        assert fields["past_posts_expected_mix_revision"] == 8

    async def test_a_handle_only_account_is_a_first_connect(self, world):
        world["account"] = facts()
        fields = await self.start(body(True, 8), account_id=ACCOUNT)
        assert fields["past_posts_entry"] == "first_connect"
        assert world["asked"][-1] == ("account", WS, ACCOUNT)

    @pytest.mark.parametrize(
        "purpose,has_past_posts",
        [("reconnect", False), ("connect", True)],
        ids=["prior-grant", "past-posts-row"],
    )
    async def test_a_reconnect_keeps_its_setting_and_takes_no_choice(
        self, world, purpose, has_past_posts
    ):
        world["account"] = facts(has_past_posts=has_past_posts)
        with pytest.raises(ConnectChoiceRefused) as refused:
            await self.start(body(False, 8), account_id=ACCOUNT, purpose=purpose)
        assert refused.value.reason == "invalid_past_posts_choice"
        assert await self.start(
            {"past_posts_choice": None}, account_id=ACCOUNT, purpose=purpose
        ) == {"past_posts_entry": "reconnect"}


class TestPreview:
    @pytest.fixture
    def rows(self, monkeypatch):
        listed = [
            {
                "source_id": "11111111-1111-4111-8111-111111111111",
                "provider": "gdrive",
                "name": "<folder>",
                "ratio": Decimal("1"),
            }
        ]

        async def sources(executor, ws):
            assert ws == WS
            return listed

        monkeypatch.setattr(versioned_mix, "sources", sources)
        return listed

    async def preview(self, *, capability=LIVE, account_id=None, purpose="connect"):
        return await connect_choice.preview(
            object(),
            workspace_id=WS,
            account_id=account_id,
            purpose=purpose,
            capability=capability,
        )

    async def test_add_is_the_contract_example(self, world, rows):
        assert await self.preview() == {
            "past_posts_available": True,
            "entry": "add",
            "account_id": None,
            "handle": None,
            "history": {"editable": True, "default": True, "saved_enabled": None},
            "expected_mix_revision": 8,
            "preview_version": 1,
            "reset": {
                "applies": True,
                "mode_after": "equal",
                "rows_after": [
                    {
                        "source_id": None,
                        "provider": "instagram_past_posts",
                        "name": None,
                        "target_share": "0.5",
                    },
                    {
                        "source_id": "11111111-1111-4111-8111-111111111111",
                        "provider": "gdrive",
                        "name": "<folder>",
                        "target_share": "0.5",
                    },
                ],
            },
        }

    async def test_the_reset_is_equal_over_enabled_sources_and_keeps_off_off(
        self, world, rows
    ):
        rows[0]["ratio"] = Decimal(".7")
        rows.append(
            {
                "source_id": "b",
                "provider": "gdrive",
                "name": "B",
                "ratio": Decimal(".3"),
            }
        )
        rows.append(
            {"source_id": "c", "provider": "gdrive", "name": "C", "ratio": Decimal(0)}
        )
        after = (await self.preview())["reset"]["rows_after"]
        shares = {r["source_id"]: Decimal(r["target_share"]) for r in after}
        assert shares["c"] == 0
        enabled = [v for k, v in shares.items() if k != "c"]
        with localcontext() as context:
            context.prec = 50
            assert sum(enabled) == 1
        assert max(enabled) - min(enabled) < Decimal("1e-48")

    async def test_a_first_connect_names_its_handle(self, world, rows):
        world["account"] = facts(handle="example_handle")
        value = await self.preview(account_id=ACCOUNT)
        assert value["entry"] == "first_connect"
        assert value["account_id"] == ACCOUNT and value["handle"] == "example_handle"
        assert value["history"] == {
            "editable": True,
            "default": True,
            "saved_enabled": None,
        }
        assert value["reset"]["rows_after"][0]["name"] == "example_handle"

    @pytest.mark.parametrize("saved", [True, False])
    async def test_a_reconnect_shows_the_setting_it_keeps(self, world, rows, saved):
        world["account"] = facts(has_past_posts=True, history_enabled=saved)
        value = await self.preview(account_id=ACCOUNT, purpose="reconnect")
        assert value["entry"] == "reconnect"
        assert value["history"] == {
            "editable": False,
            "default": None,
            "saved_enabled": saved,
        }
        assert value["reset"] is None
        assert value["expected_mix_revision"] == 8

    async def test_where_the_past_posts_is_not_live_nothing_is_a_choice(
        self, world, rows
    ):
        value = await self.preview(capability=None)
        assert value["past_posts_available"] is False
        assert value["history"] == {
            "editable": False,
            "default": False,
            "saved_enabled": None,
        }
        assert value["expected_mix_revision"] is None and value["reset"] is None

    @pytest.mark.parametrize("default", [True, False])
    async def test_a_new_connections_switch_starts_where_the_versioned_defaults_say(
        self, world, rows, monkeypatch, default
    ):
        """One definition: the preview reads `source_policy.policy_defaults()` at
        each preview and holds no copy of it."""
        real = source_policy.policy_defaults
        monkeypatch.setattr(
            source_policy,
            "policy_defaults",
            lambda: {**real(), "new_connection_use_history": default},
        )

        added = await self.preview()
        assert added["history"] == {
            "editable": True,
            "default": default,
            "saved_enabled": None,
        }
        world["account"] = facts(handle="example_handle")
        first = await self.preview(account_id=ACCOUNT)
        assert first["history"]["default"] is default
        # A reconnect has no switch to start: it names what it keeps, whatever
        # the definition says.
        world["account"] = facts(has_past_posts=True, history_enabled=True)
        kept = await self.preview(account_id=ACCOUNT, purpose="reconnect")
        assert kept["history"] == {
            "editable": False,
            "default": None,
            "saved_enabled": True,
        }


class TestRecordedChoice:
    def test_a_state_that_recorded_none_is_off(self):
        assert connect_choice.recorded_choice(
            {"past_posts_entry": "add", "past_posts_use_history": None}
        ) == PastPostsChoice(use_history=False)

    def test_a_state_written_before_the_columns_existed_is_off(self):
        assert connect_choice.recorded_choice({}) == PastPostsChoice(use_history=False)

    def test_a_recorded_choice_is_handed_on_as_recorded(self):
        assert connect_choice.recorded_choice(
            {
                "past_posts_use_history": True,
                "past_posts_expected_mix_revision": 8,
                "past_posts_preview_version": 1,
            }
        ) == PastPostsChoice(True, 8, 1)


#: The authorization the callback mints: one id and one time, which the
#: identity mapping is recorded under as well (`identity_mappings`).
AUTHORIZATION = PastPostsAuthorization(
    "99999999-9999-4999-8999-999999999999",
    "app-1",
    "17841400000000001",
    datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc),
)


class TestLandChoice:
    @pytest.fixture
    def applied(self, monkeypatch):
        seen = {}
        result = {
            "value": MembershipResult(
                "66666666-6666-4666-8666-666666666666", 3, True, 9, True, "x"
            )
        }

        async def connect_past_posts(executor, **kw):
            seen.update(kw)
            return result["value"]

        monkeypatch.setattr(connect_choice, "connect_past_posts", connect_past_posts)
        seen["result"] = result
        return seen

    async def land(self, ex, *, state=None, authorization=None):
        return await connect_choice.land_choice(
            ex,
            workspace_id=WS,
            ig_account_id=ACCOUNT,
            state=state
            or {
                "user_id": "22222222-2222-4222-8222-222222222222",
                "past_posts_use_history": True,
                "past_posts_expected_mix_revision": 8,
                "past_posts_preview_version": 1,
            },
            authorization=authorization or AUTHORIZATION,
            had_prior_grant=False,
            session_id="77777777-7777-4777-8777-777777777777",
            capability=LIVE,
        )

    async def test_the_composition_gets_the_state_choice_and_the_callbacks_authorization(
        self, applied
    ):
        applied["result"]["value"] = MembershipResult(
            "66666666-6666-4666-8666-666666666666",
            3,
            True,
            9,
            True,
            "connected_history_on",
        )
        ex = _Scripted({"id": "88888888-8888-4888-8888-888888888888"})
        assert await self.land(ex) == "88888888-8888-4888-8888-888888888888"
        assert applied["workspace_id"] == WS
        assert applied["origin_ig_account_id"] == ACCOUNT
        assert applied["choice"] == PastPostsChoice(True, 8, 1)
        assert applied["had_prior_grant"] is False
        assert applied["allocation_writer"] is versioned_mix.allocate_membership
        assert applied["capability"] is LIVE
        # The very object the callback minted: not a second id, not a second time.
        assert applied["authorization"] is AUTHORIZATION
        ((sql, params),) = ex.statements
        assert sql.startswith("INSERT INTO oauth_connection_results")
        assert "now() + make_interval(secs => :ttl)" in sql
        assert params == {
            "ws": WS,
            "uid": "22222222-2222-4222-8222-222222222222",
            "sid": "77777777-7777-4777-8777-777777777777",
            "account": ACCOUNT,
            "source": "66666666-6666-4666-8666-666666666666",
            "outcome": "connected_history_on",
            "history": True,
            "revision": 9,
            "reset": True,
            "next": None,
            "ttl": 900,
        }

    async def test_the_choice_is_applied_under_whatever_authorization_it_is_given(
        self, applied
    ):
        other = PastPostsAuthorization(
            "88888888-8888-4888-8888-888888888888",
            "app-2",
            "17841400000000002",
            datetime(2026, 10, 13, tzinfo=timezone.utc),
        )
        await self.land(_Scripted({"id": "a"}), authorization=other)
        assert applied["authorization"] is other

    async def test_the_conflict_outcome_asks_for_a_mix_review(self, applied):
        applied["result"]["value"] = MembershipResult(
            "66666666-6666-4666-8666-666666666666",
            3,
            False,
            10,
            False,
            "connected_history_off_mix_conflict",
        )
        ex = _Scripted({"id": "a"})
        await self.land(ex)
        assert ex.statements[0][1]["next"] == "review_mix"
        assert ex.statements[0][1]["outcome"] == "connected_history_off_mix_conflict"


class TestResult:
    async def test_the_read_binds_workspace_result_person_session_and_expiry(self):
        ex = _Scripted({"outcome": "connected_history_on"})
        found = await connect_choice.result(
            ex,
            workspace_id=WS,
            result_id="88888888-8888-4888-8888-888888888888",
            user_id="22222222-2222-4222-8222-222222222222",
            session_id="77777777-7777-4777-8777-777777777777",
        )
        assert found == {"outcome": "connected_history_on"}
        ((sql, params),) = ex.statements
        for predicate in (
            "r.workspace_id = :ws",
            "r.id = :id",
            "r.user_id = :uid",
            "r.session_id = :sid",
            "r.expires_at > now()",
        ):
            assert predicate in sql
        assert "w.source_mix_revision AS current_mix_revision" in sql
        assert params == {
            "ws": WS,
            "id": "88888888-8888-4888-8888-888888888888",
            "uid": "22222222-2222-4222-8222-222222222222",
            "sid": "77777777-7777-4777-8777-777777777777",
        }
