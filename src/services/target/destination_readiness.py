"""Destination readiness: the state table, as pure functions.

The mix resource lists every destination with its readiness
(`category_mix.destination_projection`), and the web renders the state, tone
and remedy it answers instead of deriving its own. The facts all come
from the database: the
destination's account and its own `ig_login` grant, each source's enablement
and whether its scope admits this destination, each archive's progress
facts (`instagram_sync.status`), and the candidates and exclusions
`category_mix.pool` counts for this destination. This module only orders those
facts. Nothing here reads, writes or judges suitability, which stays
`fn_source_asset_suitability`'s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

from src.services.target.vocabulary import (
    POLICY_EXCLUSIONS,
    READINESS_INCOMPLETE_ORDER,
    READINESS_TONES,
)

#: The account states the clock deliberately does not act on: `moved`, and
#: `disabled`, a removed destination, which the mix resource does not list.
INACTIVE_ACCOUNT_STATES = ("disabled", "moved")
#: A destination's own grant is unusable with either status
#: (`workspaces.CREDENTIAL_STATUS_SQL`). `none` is a destination never
#: connected, which needs a connection rather than a reconnect.
DEAD_CREDENTIAL_STATUSES = ("expired", "revoked")


@dataclass(frozen=True)
class SourceFacts:
    """One mix row, as one destination sees it."""

    provider: str
    #: Its ratio is above 0. Off is an explicit 0.
    enabled: bool
    #: Its scope admits this destination: a Drive folder is workspace-wide;
    #: an archive serves only its origin, through an active self-assignment.
    assignable: bool
    #: `media_sources.state`: `active`, `paused` or `error`.
    state: str = "active"
    #: An archive whose origin is this destination.
    origin_here: bool = False
    #: This destination's candidate exclusions in the source, `{reason: count}`.
    exclusions: Mapping[str, int] = field(default_factory=dict)
    #: An archive's progress facts (`instagram_sync.status`), never a Drive's.
    indexing: bool = False
    partial: bool = False
    stalled: bool = False
    needs_reconnect: bool = False

    @property
    def drawing(self) -> bool:
        """Enabled and assignable here: a source this destination draws on."""
        return self.enabled and self.assignable


@dataclass(frozen=True)
class DestinationFacts:
    #: `ig_accounts.state`.
    account_state: str
    #: Added by handle alone (`provider_account_ref` `manual:…`).
    handle_only: bool
    #: Its own `ig_login` grant: `none`, `active`, `expired` or `revoked`.
    credential_status: str
    #: The metadata candidates `category_mix.pool` counts for it.
    candidates: int
    #: Every listed source, in the mix rows' order.
    sources: Sequence[SourceFacts] = ()


def own_grant_unusable(account_state: str, credential_status: str) -> bool:
    """The destination's own grant is unusable: the account waits for a
    reconnect (`reauth_required`), or its credential died. Such a destination
    shows no supply, since nothing can be posted to it."""
    return (
        account_state == "reauth_required"
        or credential_status in DEAD_CREDENTIAL_STATUSES
    )


def all_off(enabled: Iterable[bool]) -> bool:
    """At least one source, and none of them enabled: an all-Off mix, never
    an empty one."""
    flags = list(enabled)
    return bool(flags) and not any(flags)


def leading_exclusion(facts: DestinationFacts) -> Optional[dict]:
    """The reason that excludes the most candidates from the sources this
    destination draws on, as `{reason, count}`. The reason's name breaks a
    tie. None when nothing it draws on is excluded."""
    totals: dict[str, int] = {}
    for source in facts.sources:
        if source.drawing:
            for reason, count in source.exclusions.items():
                totals[reason] = totals.get(reason, 0) + int(count)
    if not totals:
        return None
    reason = min(totals, key=lambda name: (-totals[name], name))
    return {"reason": reason, "count": totals[reason]}


def partial_coverage(facts: DestinationFacts) -> bool:
    """An archive this destination draws on has partial coverage. A separate
    fact from the state, and compatible with `ready`."""
    return any(
        source.partial
        for source in facts.sources
        if source.drawing and source.provider == "instagram_archive"
    )


def readiness(facts: DestinationFacts) -> tuple[str, str, Optional[str]]:
    """`(state, tone, remedy)` for one destination: the contract's table,
    where the first rule that matches wins."""
    state = _state(facts)
    return state, READINESS_TONES[state], _remedy(state, facts)


def _state(facts: DestinationFacts) -> str:
    if facts.account_state in INACTIVE_ACCOUNT_STATES:
        return "inactive"
    if facts.handle_only or facts.credential_status == "none":
        return "needs_connection"
    if own_grant_unusable(facts.account_state, facts.credential_status):
        return "needs_reconnect"
    if all_off(source.enabled for source in facts.sources):
        return "sources_off"
    drawing = [source for source in facts.sources if source.drawing]
    if not drawing:
        return "no_assignable_source"
    if facts.candidates > 0:
        return "ready"
    archives = [s for s in drawing if s.provider == "instagram_archive"]
    # Only an archive's origin grant can be the block here: a usable Drive
    # folder (not in `error`) means the destination is waiting on content.
    if (
        archives
        and all(archive.needs_reconnect for archive in archives)
        and not any(s.provider == "gdrive" and s.state != "error" for s in drawing)
    ):
        return "needs_reconnect"
    if any(archive.stalled for archive in archives):
        return "stalled"
    if any(source.state == "error" for source in drawing):
        return "error"
    if any(archive.indexing for archive in archives):
        return "indexing"
    return "zero_eligible"


def _remedy(state: str, facts: DestinationFacts) -> Optional[str]:
    if state == "needs_connection":
        return "connect"
    if state == "needs_reconnect":
        return "reconnect"
    if state == "no_assignable_source":
        history_off = any(
            source.provider == "instagram_archive"
            and source.origin_here
            and not source.enabled
            for source in facts.sources
        )
        return "enable_history" if history_off else "add_source"
    if state == "zero_eligible":
        leading = leading_exclusion(facts)
        if leading is not None and leading["reason"] in POLICY_EXCLUSIONS:
            return "review_policy"
        return None
    if state in ("stalled", "error"):
        return "retry"
    return None


def guidance(destinations: Sequence[Mapping], rows: Sequence[Mapping]) -> dict:
    """The workspace's rollup of its destination entries, which arrive in the
    listed account order, over its mix rows.

    `active` and `ready` count the destinations that are not `inactive`. A
    workspace whose destinations are all inactive, or whose sources are all
    Off, is `intentional`: a choice to show as one, never compulsory setup.
    `incomplete` names the active destinations that are not ready and not
    deliberately Off, in the contract's order, account order breaking ties."""
    active = [d for d in destinations if d["state"] != "inactive"]
    if destinations and not active:
        intentional = "all_inactive"
    elif all_off(row["enabled"] for row in rows):
        intentional = "all_sources_off"
    else:
        intentional = None
    # `sorted` is stable, so destinations in one state keep the account order.
    incomplete = sorted(
        (d for d in active if d["state"] in READINESS_INCOMPLETE_ORDER),
        key=lambda d: READINESS_INCOMPLETE_ORDER.index(d["state"]),
    )
    return {
        "active": len(active),
        "ready": sum(d["state"] == "ready" for d in active),
        "intentional": intentional,
        "incomplete": [
            {"account_id": d["account_id"], "state": d["state"], "remedy": d["remedy"]}
            for d in incomplete
        ],
    }
