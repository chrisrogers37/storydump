"""The destination readiness table, decided from facts alone.

`destination_readiness` is pure: facts in, `(state, tone, remedy)` out. Each
rule of the contract's table gets a case it decides, and a control showing the
next rule takes over once the fact is gone, so the order itself is pinned.
"""

from pathlib import Path

import pytest

from src.services.target import destination_readiness as dr
from src.services.target.vocabulary import (
    POLICY_EXCLUSIONS,
    READINESS_INCOMPLETE_ORDER,
    READINESS_INTENTIONAL,
    READINESS_REMEDIES,
    READINESS_TONES,
)

ROOT = Path(__file__).resolve().parents[4]


def drive(**kw):
    kw.setdefault("enabled", True)
    return dr.SourceFacts(provider="gdrive", assignable=True, **kw)


def archive(**kw):
    """An archive whose origin is this destination unless told otherwise."""
    kw.setdefault("enabled", True)
    kw.setdefault("assignable", True)
    kw.setdefault("origin_here", kw["assignable"])
    return dr.SourceFacts(provider="instagram_archive", **kw)


def destination(*sources, candidates=0, **kw):
    kw.setdefault("account_state", "active")
    kw.setdefault("handle_only", False)
    kw.setdefault("credential_status", "active")
    return dr.DestinationFacts(candidates=candidates, sources=sources, **kw)


#: A destination every later rule would call ready: a drawing source with supply.
READY = dict(candidates=3)


class TestTheStateTableInOrder:
    @pytest.mark.parametrize("state", ["moved", "disabled"])
    def test_1_an_inactive_account_is_inactive_whatever_its_supply(self, state):
        facts = destination(drive(), account_state=state, **READY)
        assert dr.readiness(facts) == ("inactive", "inert", None)

    @pytest.mark.parametrize(
        "kw",
        [
            {"handle_only": True, "credential_status": "none"},
            {"credential_status": "none"},
            # Before its own grant: a handle-only destination has none to renew.
            {"handle_only": True, "account_state": "reauth_required"},
        ],
    )
    def test_2_handle_only_or_never_credentialed_needs_connection(self, kw):
        facts = destination(drive(), **READY, **kw)
        assert dr.readiness(facts) == ("needs_connection", "attention", "connect")

    @pytest.mark.parametrize(
        "kw",
        [
            {"account_state": "reauth_required"},
            {"credential_status": "expired"},
            {"credential_status": "revoked"},
        ],
    )
    def test_3_an_unusable_own_grant_needs_reconnect_before_sources(self, kw):
        for sources in ([drive(), archive()], [drive(enabled=False)]):
            facts = destination(*sources, **READY, **kw)
            assert dr.readiness(facts) == ("needs_reconnect", "attention", "reconnect")

    def test_a_connected_destination_is_not_held_by_its_grant(self):
        # Control for 2 and 3: an active account with an active grant goes on.
        assert dr.readiness(destination(drive(), **READY))[0] == "ready"

    def test_4_every_source_off_is_sources_off_even_if_this_one_owns_one(self):
        facts = destination(drive(enabled=False), archive(enabled=False), **READY)
        assert dr.readiness(facts) == ("sources_off", "inert", None)

    def test_5_one_source_on_elsewhere_is_not_sources_off(self):
        # Another origin's archive is on: the mix is not all Off, but nothing
        # this destination may draw on is.
        other = archive(assignable=False, enabled=True)
        facts = destination(other, drive(enabled=False))
        assert dr.readiness(facts) == (
            "no_assignable_source",
            "attention",
            "add_source",
        )

    def test_5_no_source_at_all_needs_a_source(self):
        assert dr.readiness(destination()) == (
            "no_assignable_source",
            "attention",
            "add_source",
        )

    def test_5_its_own_history_off_is_the_remedy_when_it_has_one(self):
        facts = destination(archive(assignable=False), archive(enabled=False))
        assert dr.readiness(facts) == (
            "no_assignable_source",
            "attention",
            "enable_history",
        )

    def test_5_its_own_history_already_on_is_not_the_remedy(self):
        # Its origin is here and it is On, yet it does not serve this
        # destination: enabling it is no remedy, since it is enabled already.
        already_on = archive(assignable=False, origin_here=True, enabled=True)
        assert dr.readiness(destination(already_on)) == (
            "no_assignable_source",
            "attention",
            "add_source",
        )
        # Control: the same history Off, beside another origin's, is the one
        # to enable.
        off = destination(archive(assignable=False), archive(enabled=False))
        assert dr.readiness(off)[2] == "enable_history"

    def test_5_an_off_history_of_another_origin_is_not_its_remedy(self):
        mine_elsewhere = archive(enabled=False, assignable=False, origin_here=False)
        facts = destination(archive(assignable=False), mine_elsewhere)
        assert dr.readiness(facts)[2] == "add_source"

    def test_6_candidates_make_it_ready_before_any_archive_trouble(self):
        troubled = archive(stalled=True, indexing=True, needs_reconnect=True)
        facts = destination(troubled, drive(state="error"), **READY)
        assert dr.readiness(facts) == ("ready", "active", None)

    def test_6_one_candidate_is_supply_enough(self):
        # The boundary of rule 6, with the case just under it as its control.
        assert dr.readiness(destination(drive(), candidates=1)) == (
            "ready",
            "active",
            None,
        )
        assert dr.readiness(destination(drive(), candidates=0))[0] == "zero_eligible"

    def test_7_every_drawing_archive_needing_reconnect_with_no_usable_drive(self):
        facts = destination(archive(needs_reconnect=True, stalled=True))
        assert dr.readiness(facts) == ("needs_reconnect", "attention", "reconnect")
        # A Drive folder in error is not a usable one.
        facts = destination(archive(needs_reconnect=True), drive(state="error"))
        assert dr.readiness(facts)[0] == "needs_reconnect"

    def test_7_a_usable_drive_means_it_waits_on_content_not_a_reconnect(self):
        for state in ("active", "paused"):
            facts = destination(archive(needs_reconnect=True), drive(state=state))
            assert dr.readiness(facts)[0] == "zero_eligible"

    def test_7_one_healthy_archive_is_enough_to_skip_it(self):
        facts = destination(archive(needs_reconnect=True), archive(indexing=True))
        assert dr.readiness(facts)[0] == "indexing"

    def test_7_needs_an_archive_so_a_drive_in_error_is_an_error(self):
        assert dr.readiness(destination(drive(state="error")))[0] == "error"

    def test_8_a_stalled_archive_is_stalled_before_error_and_indexing(self):
        facts = destination(archive(stalled=True, indexing=True), drive(state="error"))
        assert dr.readiness(facts) == ("stalled", "problem", "retry")

    def test_8_only_a_drawing_archive_counts_as_stalled(self):
        off = archive(enabled=False, stalled=True)
        facts = destination(off, drive())
        assert dr.readiness(facts)[0] == "zero_eligible"

    def test_9_a_drawing_source_in_error_is_an_error_before_indexing(self):
        facts = destination(archive(indexing=True), drive(state="error"))
        assert dr.readiness(facts) == ("error", "problem", "retry")
        # An archive in error also needs a reconnect (`instagram_sync.status`),
        # but a usable Drive folder keeps rule 7 from naming it.
        facts = destination(archive(state="error", needs_reconnect=True), drive())
        assert dr.readiness(facts)[0] == "error"

    def test_9_an_off_source_in_error_is_not_this_ones_error(self):
        facts = destination(drive(state="error", enabled=False), archive(indexing=True))
        assert dr.readiness(facts)[0] == "indexing"

    def test_10_an_archive_still_indexing_is_progress(self):
        facts = destination(archive(indexing=True), drive())
        assert dr.readiness(facts) == ("indexing", "progress", None)

    def test_10_another_destinations_archive_indexing_is_not_this_ones_progress(self):
        # Enabled and being indexed, but its origin is elsewhere: it does not
        # serve this destination, so this one is still waiting on content.
        elsewhere = archive(assignable=False, indexing=True)
        assert dr.readiness(destination(elsewhere, drive()))[0] == "zero_eligible"
        # Control: the same archive, serving this destination, is progress.
        here = archive(assignable=True, indexing=True)
        assert dr.readiness(destination(here, drive()))[0] == "indexing"

    def test_11_otherwise_zero_eligible(self):
        assert dr.readiness(destination(archive(), drive())) == (
            "zero_eligible",
            "attention",
            None,
        )


class TestRemedies:
    @pytest.mark.parametrize("reason", POLICY_EXCLUSIONS)
    def test_a_leading_policy_exclusion_asks_to_review_the_policy(self, reason):
        facts = destination(archive(exclusions={reason: 5, "locked": 2}))
        assert dr.readiness(facts) == ("zero_eligible", "attention", "review_policy")

    @pytest.mark.parametrize(
        "exclusions", [{}, {"locked": 5, "too_recent": 2}, {"live_intent": 1}]
    )
    def test_a_leading_lock_or_nothing_names_no_remedy(self, exclusions):
        facts = destination(archive(exclusions=exclusions))
        assert dr.readiness(facts) == ("zero_eligible", "attention", None)

    def test_an_excluded_source_it_does_not_draw_on_names_nothing(self):
        other = archive(assignable=False, exclusions={"too_recent": 9})
        facts = destination(other, drive(exclusions={"locked": 1}))
        assert dr.readiness(facts)[2] is None

    def test_every_remedy_is_one_the_vocabulary_names_and_each_is_reachable(self):
        cases = [
            destination(drive(), handle_only=True),
            destination(drive(), account_state="reauth_required"),
            destination(archive(assignable=False), archive(enabled=False)),
            destination(),
            destination(archive(exclusions={"too_recent": 1})),
            destination(archive(stalled=True)),
        ]
        remedies = [dr.readiness(facts)[2] for facts in cases]
        assert remedies == list(READINESS_REMEDIES)


class TestTones:
    def test_each_state_renders_with_the_contracts_tone(self):
        assert READINESS_TONES == {
            "inactive": "inert",
            "needs_connection": "attention",
            "needs_reconnect": "attention",
            "sources_off": "inert",
            "no_assignable_source": "attention",
            "ready": "active",
            "stalled": "problem",
            "error": "problem",
            "indexing": "progress",
            "zero_eligible": "attention",
        }

    def test_every_state_the_table_reaches_has_its_tone(self):
        reached = {
            dr.readiness(facts)
            for facts in (
                destination(drive(), account_state="moved"),
                destination(drive(), credential_status="none"),
                destination(drive(), credential_status="revoked"),
                destination(drive(enabled=False)),
                destination(),
                destination(drive(), **READY),
                destination(archive(stalled=True)),
                destination(drive(state="error")),
                destination(archive(indexing=True)),
                destination(archive()),
            )
        }
        assert {state for state, _, _ in reached} == set(READINESS_TONES)
        assert all(READINESS_TONES[state] == tone for state, tone, _ in reached)


class TestPartialCoverage:
    def test_it_is_a_separate_fact_compatible_with_ready(self):
        facts = destination(archive(partial=True), **READY)
        assert dr.readiness(facts)[0] == "ready" and dr.partial_coverage(facts)

    def test_it_reads_only_archives_this_destination_draws_on(self):
        assert not dr.partial_coverage(destination(archive(), **READY))
        assert not dr.partial_coverage(
            destination(archive(partial=True, enabled=False), drive(), **READY)
        )
        assert not dr.partial_coverage(
            destination(archive(partial=True, assignable=False), drive(), **READY)
        )
        assert not dr.partial_coverage(destination(drive(partial=True), **READY))


class TestLeadingExclusion:
    def test_the_largest_total_across_drawing_sources_leads(self):
        facts = destination(
            archive(exclusions={"too_recent": 3, "locked": 2}),
            drive(exclusions={"locked": 2, "unavailable": 1}),
            archive(assignable=False, exclusions={"too_recent": 50}),
        )
        assert dr.leading_exclusion(facts) == {"reason": "locked", "count": 4}

    def test_the_reasons_name_breaks_a_tie(self):
        facts = destination(drive(exclusions={"unavailable": 2, "live_intent": 2}))
        assert dr.leading_exclusion(facts) == {"reason": "live_intent", "count": 2}

    def test_nothing_excluded_is_none(self):
        assert dr.leading_exclusion(destination(drive(), archive())) is None
        assert dr.leading_exclusion(destination()) is None


def entry(account, state, remedy=None):
    return {"account_id": account, "state": state, "remedy": remedy}


class TestGuidance:
    ROWS_ON = [{"enabled": True}, {"enabled": False}]

    def test_incomplete_follows_the_contracts_order_then_account_order(self):
        destinations = [
            entry("a", "zero_eligible"),
            entry("b", "needs_connection", "connect"),
            entry("c", "indexing"),
            entry("d", "needs_connection", "connect"),
            entry("e", "ready"),
            entry("f", "inactive"),
            entry("g", "sources_off"),
            entry("h", "no_assignable_source", "add_source"),
            entry("i", "error", "retry"),
            entry("j", "stalled", "retry"),
            entry("k", "needs_reconnect", "reconnect"),
        ]
        result = dr.guidance(destinations, self.ROWS_ON)
        assert [d["account_id"] for d in result["incomplete"]] == [
            "b",
            "d",
            "k",
            "h",
            "j",
            "i",
            "c",
            "a",
        ]
        assert result["incomplete"][0] == entry("b", "needs_connection", "connect")
        assert (result["active"], result["ready"], result["intentional"]) == (
            10,
            1,
            None,
        )
        assert list(READINESS_INCOMPLETE_ORDER) == [
            "needs_connection",
            "needs_reconnect",
            "no_assignable_source",
            "stalled",
            "error",
            "indexing",
            "zero_eligible",
        ]

    def test_a_tie_follows_the_listed_account_order_never_the_id(self):
        # The ids are arbitrary: the destinations arrive oldest first, and two
        # in one state stay in that order whatever their ids sort to.
        destinations = [
            entry("m-2", "needs_connection", "connect"),
            entry("z-1", "zero_eligible"),
            entry("a-3", "needs_connection", "connect"),
            entry("c-4", "zero_eligible"),
            entry("b-5", "needs_connection", "connect"),
        ]
        result = dr.guidance(destinations, self.ROWS_ON)
        assert [d["account_id"] for d in result["incomplete"]] == [
            "m-2",
            "a-3",
            "b-5",
            "z-1",
            "c-4",
        ]
        # Control: reversing the listed order reverses each tie.
        result = dr.guidance(destinations[::-1], self.ROWS_ON)
        assert [d["account_id"] for d in result["incomplete"]] == [
            "b-5",
            "a-3",
            "m-2",
            "c-4",
            "z-1",
        ]

    def test_every_destination_inactive_is_intentional_and_beats_all_off(self):
        destinations = [entry("a", "inactive"), entry("b", "inactive")]
        result = dr.guidance(destinations, [{"enabled": False}])
        assert result == {
            "active": 0,
            "ready": 0,
            "intentional": "all_inactive",
            "incomplete": [],
        }

    def test_every_source_off_is_intentional_and_never_incomplete(self):
        destinations = [entry("a", "sources_off"), entry("b", "inactive")]
        result = dr.guidance(destinations, [{"enabled": False}, {"enabled": False}])
        assert result == {
            "active": 1,
            "ready": 0,
            "intentional": "all_sources_off",
            "incomplete": [],
        }

    def test_no_source_or_no_destination_is_setup_not_intent(self):
        assert (
            dr.guidance([entry("a", "no_assignable_source", "add_source")], [])[
                "intentional"
            ]
            is None
        )
        assert dr.guidance([], self.ROWS_ON)["intentional"] is None
        assert dr.guidance([], []) == {
            "active": 0,
            "ready": 0,
            "intentional": None,
            "incomplete": [],
        }

    def test_one_ready_destination_cannot_complete_the_workspace(self):
        destinations = [entry("a", "ready"), entry("b", "indexing")]
        result = dr.guidance(destinations, self.ROWS_ON)
        assert (result["active"], result["ready"]) == (2, 1)
        assert result["incomplete"] == [entry("b", "indexing")]

    def test_the_intentional_states_are_the_vocabularys(self):
        assert READINESS_INTENTIONAL == ("all_inactive", "all_sources_off")


def test_the_policy_reasons_are_spelled_as_the_suitability_relation_returns_them():
    # The remedy keys on these strings, so a respelling in the database would
    # silently stop naming `review_policy`. Read them back from the current authority's body.
    body = (ROOT / "scripts/migrations/147_archive_asset_suitability.sql").read_text()
    for reason in POLICY_EXCLUSIONS:
        assert f"THEN '{reason}'" in body, reason
