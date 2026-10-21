"""The history choice an Instagram connect carries through OAuth.

Before OAuth a person chooses whether the account's past posts
become a content source. The start records that choice in its one-shot state
(`oauth_states`), the callback applies it through
`source_membership.connect_past_posts` in the unit of work that lands the
destination and its credential, and one `oauth_connection_results` row
records what was applied, for the Settings banner to read back.

Live only where the past-posts source is: a capability the trusted composition root
(`past_posts_composition.build_api`) injects where the past-posts source is armed, and a
converted mix. By default none is injected, so there every answer here is
"not live", reached without a statement, and the connect leg accepts no
choice: nothing recorded, no result, `?connected=instagram`.

What a choice DOES is `connect_past_posts`'s, unchanged: a new account lands On
or Off as chosen, an existing past-posts source keeps its saved setting, a pre-rollout
grant lands Off, and a stale preview lands Off with
`connected_history_off_mix_conflict`. This module owns only the wire: the
preview, the start's refusals, what the state records, and the result.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from src.exceptions.base import RefusalError
from src.exceptions.mix import MixInvalid
from src.services.target import readers, source_policy, source_state, versioned_mix
from src.services.target.instagram_sources import PastPostsAuthorization, is_activated
from src.services.target.source_membership import PastPostsChoice, connect_past_posts
from src.services.target.versioned_mix import MixConflict

#: The one preview shape a start may say it was shown.
PREVIEW_VERSION = 1

#: Results expire after 15 minutes.
RESULT_TTL_SECONDS = 900

#: `oauth_states.past_posts_expected_mix_revision` is a BIGINT.
REVISION_MAX = 2**63 - 1

#: The one outcome that asks the person to act: review the mix, then enable.
NEXT_ACTION = {"connected_history_off_mix_conflict": "review_mix"}

#: The body a start accepts, and the keys of its choice.
BODY_KEYS = frozenset({"past_posts_choice"})
CHOICE_KEYS = frozenset({"use_history", "expected_mix_revision", "preview_version"})

#: The share arithmetic's key for the past-posts source the preview would create, which
#: has no id yet. Empty, so it sorts before every id.
PROSPECTIVE = ""

#: `ConnectChoiceRefused.reason`, closed.
REASONS = ("invalid_past_posts_choice", "past_posts_not_activated")


class ConnectChoiceRefused(RefusalError):
    """A start's history choice refused, by name. ``reason`` is one of
    :data:`REASONS`: ``invalid_past_posts_choice`` (the body is not the contract's
    shape, or it carries a choice on a reconnect, which keeps its saved
    setting) or ``past_posts_not_activated`` (a choice where the past-posts source is not
    live)."""

    _prefix = "past-posts choice refused"

    def __init__(self, reason: str, detail: str = ""):
        if reason not in REASONS:
            raise ValueError(f"unknown ConnectChoiceRefused reason {reason!r}")
        super().__init__(reason, detail)


def _count(value: Any, *, high: int) -> bool:
    """An integer in ``0..high`` — never a bool, never a float."""
    return type(value) is int and 0 <= value <= high


def parse_choice(body: Any) -> Optional[PastPostsChoice]:
    """The choice a start body carries, or None for none.

    ``{"past_posts_choice": {"use_history": bool, "expected_mix_revision": int,
    "preview_version": 1}}``. An absent body, ``{}`` and ``{"past_posts_choice":
    null}`` carry none: a start with no choice. Anything else — an unknown key
    at either level, a missing one, a wrong type, another preview version — is
    ``invalid_past_posts_choice`` before anything is read."""
    if not isinstance(body, dict) or set(body) - BODY_KEYS:
        raise ConnectChoiceRefused("invalid_past_posts_choice")
    raw = body.get("past_posts_choice")
    if raw is None:
        return None
    if (
        not isinstance(raw, dict)
        or set(raw) != CHOICE_KEYS
        or type(raw["use_history"]) is not bool
        or not _count(raw["expected_mix_revision"], high=REVISION_MAX)
        or type(raw["preview_version"]) is not int
        or raw["preview_version"] != PREVIEW_VERSION
    ):
        raise ConnectChoiceRefused("invalid_past_posts_choice")
    return PastPostsChoice(
        raw["use_history"], raw["expected_mix_revision"], raw["preview_version"]
    )


async def live_revision(executor, *, workspace_id: str, capability) -> Optional[int]:
    """The mix revision a history choice is checked against, or None where the
    past-posts source is not live here: no capability injected (the default, where this
    issues no statement) or a mix not yet converted.

    With a capability the canonical source prefix is taken first
    (`source_state.lock_workspace_sources`), so the revision and what the
    caller reads next are one view, and a callback that goes on to lock its
    destination and credential rows takes them after the prefix, in the order
    every source writer keeps."""
    if not is_activated(capability):
        return None
    ws = str(workspace_id)
    await source_state.lock_workspace_sources(executor, workspace_id=ws)
    saved = await versioned_mix.state(executor, ws)
    return int(saved["source_mix_revision"]) if versioned_mix.converted(saved) else None


async def _account(executor, ws: str, account_id: str) -> Optional[dict]:
    """A named destination's handle and its past-posts source's saved setting."""
    return await readers.row(
        executor,
        "SELECT a.handle, s.id IS NOT NULL AS has_past_posts,"
        "       COALESCE(x.ratio > 0, false) AS history_enabled"
        "  FROM ig_accounts a"
        "  LEFT JOIN media_sources s ON s.workspace_id = a.workspace_id"
        "   AND s.origin_ig_account_id = a.id AND s.provider = 'instagram_past_posts'"
        "  LEFT JOIN category_post_case_mix x ON x.workspace_id = s.workspace_id"
        "   AND x.source_id = s.id AND x.effective_to IS NULL"
        " WHERE a.workspace_id = :ws AND a.id = :account",
        ws=ws,
        account=str(account_id),
    )


def _entry(account: Optional[dict], purpose: str) -> str:
    """``add`` names no account; ``reconnect`` names one with a prior grant
    (the start's own ``reconnect`` purpose) or a past-posts source; ``first_connect``
    names one with neither."""
    if account is None:
        return "add"
    if purpose == "reconnect" or account["has_past_posts"]:
        return "reconnect"
    return "first_connect"


async def _reset(executor, ws: str, account: Optional[dict]) -> dict:
    """The mix an On choice would leave: equal over the enabled sources and the
    new past-posts source, Off kept Off — the allocation's own `versioned_mix.equal`,
    over the rows the mix resource lists."""
    rows = await versioned_mix.sources(executor, ws)
    if any(r["ratio"] is None for r in rows):
        raise MixInvalid("source_set")
    shares = versioned_mix.equal(
        {PROSPECTIVE: Decimal(1), **{r["source_id"]: r["ratio"] for r in rows}}
    )
    prospective = {
        "source_id": None,
        "provider": "instagram_past_posts",
        "name": None if account is None else account["handle"],
        "target_share": versioned_mix.ratio_string(shares[PROSPECTIVE]),
    }
    return {
        "applies": True,
        "mode_after": "equal",
        "rows_after": [prospective]
        + [
            {
                "source_id": r["source_id"],
                "provider": r["provider"],
                "name": r["name"],
                "target_share": versioned_mix.ratio_string(shares[r["source_id"]]),
            }
            for r in rows
        ],
    }


async def preview(
    executor,
    *,
    workspace_id: str,
    account_id: Optional[str] = None,
    purpose: str = "connect",
    capability=None,
) -> dict:
    """What connecting would do to the history choice and the mix. Writes
    nothing. *purpose* is the start's own (`ig_login_oauth.connect_purpose`)
    for a named account; the caller has resolved that account as this
    workspace's."""
    ws = str(workspace_id)
    revision = await live_revision(executor, workspace_id=ws, capability=capability)
    account = None if account_id is None else await _account(executor, ws, account_id)
    entry = _entry(account, purpose)
    choosing = revision is not None and entry != "reconnect"
    if entry == "reconnect":
        history = {
            "editable": False,
            "default": None,
            "saved_enabled": bool(account["history_enabled"]),
        }
    else:
        history = {
            "editable": choosing,
            # Where Use my past posts starts for a new connection: the
            # backend's one versioned default definition, read
            # here and never copied.
            "default": choosing
            and source_policy.policy_defaults()["new_connection_use_history"],
            "saved_enabled": None,
        }
    return {
        "past_posts_available": revision is not None,
        "entry": entry,
        "account_id": None if account_id is None else str(account_id),
        "handle": None if account is None else account["handle"],
        "history": history,
        "expected_mix_revision": revision,
        "preview_version": PREVIEW_VERSION,
        "reset": await _reset(executor, ws, account) if choosing else None,
    }


async def start_fields(
    executor,
    *,
    workspace_id: str,
    choice: Optional[PastPostsChoice],
    account_id: Optional[str] = None,
    purpose: str = "connect",
    capability=None,
) -> dict:
    """What a start records about the history choice — `issue_state`'s
    ``past_posts_*`` keywords — or nothing where the past-posts source is not live.

    *choice* is what :func:`parse_choice` made of the body, which the route read
    and judged before it opened the session this runs in: a body not of the
    contract's shape (`invalid_past_posts_choice`, 400) never gets here. Refused
    in the contract's order, all before any state is issued: a choice where the
    past-posts source is not live (`past_posts_not_activated`, 503); a choice on a reconnect,
    which keeps its saved setting (`invalid_past_posts_choice`); and On against a
    revision the mix has moved past (`MixConflict`, 409). Off is never stale: an
    Off add resets nothing."""
    ws = str(workspace_id)
    revision = await live_revision(executor, workspace_id=ws, capability=capability)
    if revision is None:
        if choice is not None:
            raise ConnectChoiceRefused("past_posts_not_activated")
        return {}
    account = None if account_id is None else await _account(executor, ws, account_id)
    entry = _entry(account, purpose)
    if choice is None:
        return {"past_posts_entry": entry}
    if entry == "reconnect":
        raise ConnectChoiceRefused(
            "invalid_past_posts_choice", "a reconnect keeps its saved history setting"
        )
    if choice.use_history and choice.expected_mix_revision != revision:
        raise MixConflict(revision)
    return {
        "past_posts_entry": entry,
        "past_posts_use_history": choice.use_history,
        "past_posts_expected_mix_revision": choice.expected_mix_revision,
        "past_posts_preview_version": choice.preview_version,
    }


def recorded_choice(state: dict) -> PastPostsChoice:
    """The choice a consumed state recorded. A state that recorded none is
    Off: a new account with no recorded choice does not use its history."""
    if state.get("past_posts_use_history") is None:
        return PastPostsChoice(use_history=False)
    return PastPostsChoice(
        state["past_posts_use_history"],
        state["past_posts_expected_mix_revision"],
        state["past_posts_preview_version"],
    )


async def land_choice(
    executor,
    *,
    workspace_id: str,
    ig_account_id: str,
    state: dict,
    authorization: PastPostsAuthorization,
    had_prior_grant: bool,
    session_id: str,
    capability,
) -> str:
    """Apply the recorded choice where the destination just landed, and record
    what was applied. Returns the result's id.

    In the callback's unit of work, after the credential: `connect_past_posts`
    decides, and a `PastPostsRefused` it raises rolls the whole landing back.
    The authorization is the exchange that just succeeded, minted by the
    callback: a fresh id, the configured app, the grant's account and the
    callback time. The callback also records the verified app/subject mapping
    (`identity_mappings`) under that same id and time. The result is bound to
    the workspace, the state's user and the returning session."""
    applied = await connect_past_posts(
        executor,
        workspace_id=str(workspace_id),
        origin_ig_account_id=str(ig_account_id),
        authorization=authorization,
        had_prior_grant=had_prior_grant,
        choice=recorded_choice(state),
        allocation_writer=versioned_mix.allocate_membership,
        capability=capability,
    )
    row = await readers.row(
        executor,
        "INSERT INTO oauth_connection_results"
        " (workspace_id, user_id, session_id, ig_account_id, source_id, outcome,"
        "  history_enabled, mix_revision, mix_reset_applied, next_action, expires_at)"
        " VALUES (:ws, :uid, :sid, :account, :source, :outcome, :history,"
        "         :revision, :reset, :next, now() + make_interval(secs => :ttl))"
        " RETURNING id",
        ws=str(workspace_id),
        uid=str(state["user_id"]),
        sid=str(session_id),
        account=str(ig_account_id),
        source=applied.source_id,
        outcome=applied.outcome,
        history=applied.history_enabled,
        revision=applied.mix_revision,
        reset=applied.mix_reset_applied,
        next=NEXT_ACTION.get(applied.outcome),
        ttl=RESULT_TTL_SECONDS,
    )
    return str(row["id"])


async def result(
    executor,
    *,
    workspace_id: str,
    result_id: str,
    user_id: str,
    session_id: Optional[str],
) -> Optional[dict]:
    """One applied result, for the person and the session it is bound to, or
    None — the same None for another person, another session, another
    workspace, an expired result and no result at all. Carries the mix's
    current revision, so a reader can tell a historical result from current
    status."""
    return await readers.row(
        executor,
        "SELECT r.ig_account_id::text AS account_id, r.source_id::text AS source_id,"
        "       r.outcome, r.history_enabled, r.mix_revision, r.mix_reset_applied,"
        "       r.next_action, w.source_mix_revision AS current_mix_revision,"
        "       r.created_at"
        "  FROM oauth_connection_results r"
        "  JOIN workspaces w ON w.id = r.workspace_id"
        " WHERE r.workspace_id = :ws AND r.id = :id AND r.user_id = :uid"
        "   AND r.session_id = :sid AND r.expires_at > now()",
        ws=str(workspace_id),
        id=str(result_id),
        uid=str(user_id),
        sid=session_id,
    )
