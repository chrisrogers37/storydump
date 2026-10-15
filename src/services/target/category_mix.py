"""The posting mix — `category_post_case_mix`, a Type 2 SCD table — keyed
on the CONNECTED FOLDER (sources are the groups),
and the ONE service that writes it.

A workspace connects folders (`media_sources`); everything inside a connected
folder syncs, at any depth (the walk of #1256); and the workspace weights how
often each connected folder posts: announcements 70 / behind-the-scenes 30. To
weight two subfolders separately, connect each as its own folder. `scheduler.
execute_plan_slot` draws a folder by these weights before it picks a file —
through :func:`weights`, the ONE function that also feeds the card's
"Posts about" column, so the two can never disagree.

The draw's read lives here with its arithmetic: :func:`pool` is the folders a
draw can land on and the files eligible in each (:data:`ELIGIBLE_SQL`), and
the Overview's days of content left (`content_runway`) counts
the same read, so the figure and the draw cannot disagree either.

The table keeps its row shape (one row per source per effective period;
`effective_to IS NULL` is the current row, `uq_case_mix_current_by_source`
makes two current rows for a source impossible) and **sum-to-one is
service-enforced HERE**. Setting a mix is one supersede (close every current
row) then one insert per source, in the caller's transaction. Ratios are
fractions in [0, 1]; a **0 is Off** (the folder stays connected and synced,
never posts, and shapes nothing); the ratios above 0 sum to 1 within a
rounding tolerance; the web converts percentages. An empty mix is legal and
means every folder is automatic.

`category` stays on the row as the LABEL the card shows — the source's
folder name at save time — never a key. A row written before 071 carries no
`source_id`: the planner ignores it, the first save supersedes it.
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

from src.exceptions.mix import MixInvalid
from src.services.target import destination_readiness
from src.services.target import intent_ledger, readers
from src.services.target.workspaces import (
    CONNECTED_SQL,
    CREDENTIAL_STATUS_SQL,
    IG_LOGIN_PROVIDER,
    LISTED_ACCOUNT_ORDER_SQL,
    LISTED_ACCOUNT_SQL,
)

logger = logging.getLogger(__name__)

#: Sum-to-one tolerance: four decimal places per row, so a three-way split
#: cannot hit 1.0000 exactly and must not be refused for it.
SUM_TOLERANCE = 0.001
#: More rows than this is not a mix anyone typed by hand; refused by name.
MAX_SOURCES = 500

#: The label a source row shows — its folder name, else its ref. Every query
#: here aliases `media_sources` as `s`, as `CONNECTED_SQL` expects.
_LABEL = "COALESCE(s.config->>'folder_name', s.config->>'folder_ref', 'folder')"

#: A source's CURRENT mix row, `x`, joined after ``FROM media_sources s``: the
#: row keyed on that source, in the source's own workspace, still in effect.
#: :func:`pool_folders` and :func:`mix_view` both read the mix through it.
_CURRENT_MIX_JOIN = (
    "  LEFT JOIN category_post_case_mix x"
    "    ON x.workspace_id = s.workspace_id AND x.source_id = s.id"
    "   AND x.effective_to IS NULL"
)


def _ratio(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        # NaN passes every comparison and NUMERIC stores it; the sum-to-one
        # invariant would be bypassable by one JSON `NaN` (#1251).
        raise MixInvalid("bad_ratio", label)
    ratio = round(float(value), 4)
    if ratio < 0 or ratio > 1:
        raise MixInvalid("bad_ratio", label)
    return ratio


def _sum_to_one(rows: list[tuple[str, float]]) -> None:
    positives = [r for _, r in rows if r > 0]
    total = round(sum(positives), 4)
    if positives and round(abs(total - 1.0), 4) > SUM_TOLERANCE:
        raise MixInvalid("sum_not_one", f"sum is {total:.4f}")


def normalize(mix: Any) -> list[tuple[str, float]]:
    """Validate and normalize `[{"source_id", "ratio"}, …]` to
    `[(source_id, ratio)]` with ratios rounded to the table's four places.
    Refuses by name; an empty list is legal; a 0 is Off and is kept."""
    if not isinstance(mix, list):
        raise MixInvalid("not_a_list")
    if len(mix) > MAX_SOURCES:
        raise MixInvalid("too_many_sources", str(len(mix)))
    rows: list[tuple[str, float]] = []
    seen: set[str] = set()
    for entry in mix:
        if not isinstance(entry, dict):
            raise MixInvalid("not_a_list", "each entry must be an object")
        source_id = entry.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise MixInvalid("empty_source")
        source_id = source_id.strip()
        if source_id in seen:
            raise MixInvalid("duplicate_source", source_id)
        seen.add(source_id)
        rows.append((source_id, _ratio(entry.get("ratio"), source_id)))
    _sum_to_one(rows)
    return rows


def weights(rows: list[dict]) -> dict[str, float]:
    """The draw's arithmetic — ONE function for the planner and the card.

    *rows*: `{"source_id", "ratio": float | None, "n": eligible media}` per
    connected folder. Explicit rows (ratio > 0) share by ratio; automatic rows
    (no row: ratio None) share the rest in proportion to their media; together
    the automatic rows never take more than the smallest explicit weight on
    the FINAL split: with explicit weights
    summing to one and `r_min` the smallest of the explicit rows that have
    media, the automatic pool is `A = min(share_auto, r_min / (1 + r_min))`.
    With no explicit rows everything is automatic (`A = 1`). Off (ratio 0)
    and a row with no eligible media weigh 0 and shape nothing. Returns a
    weight per source_id; they sum to 1 when anything is drawable.
    """
    shaped = [
        (
            str(r["source_id"]),
            None if r.get("ratio") is None else float(r["ratio"]),
            int(r["n"]),
        )
        for r in rows
    ]
    out = {sid: 0.0 for sid, _, _ in shaped}
    explicit = [(sid, ratio, n) for sid, ratio, n in shaped if ratio and n > 0]
    auto = [(sid, n) for sid, ratio, n in shaped if ratio is None and n > 0]
    n_auto = sum(n for _, n in auto)
    total_ratio = sum(ratio for _, ratio, _ in explicit)
    if not explicit:
        pool = 1.0
    elif not auto:
        pool = 0.0
    else:
        r_min = min(ratio / total_ratio for _, ratio, _ in explicit)
        share_auto = n_auto / (n_auto + sum(n for _, _, n in explicit))
        pool = min(share_auto, r_min / (1 + r_min))
    for sid, n in auto:
        out[sid] = pool * n / n_auto
    for sid, ratio, _ in explicit:
        out[sid] = (1 - pool) * ratio / total_ratio
    return out


#: The terminal intent states as a SQL list, spelled from their one home,
#: `intent_ledger.TERMINAL_STATES`: an intent in one of them holds no file.
_TERMINAL_STATES_SQL = ", ".join(
    f"'{state}'" for state in intent_ledger.TERMINAL_STATES
)

#: Eligibility in full, spliced after ``FROM media_items m``: available, not
#: already live for this account (an intent of its own not yet terminal), minus
#: the workspace-wide locks (skip/reject/hold/seasonal/unsupported) and minus
#: THIS account's own `recent` locks — a live lock is one with no expiry or an
#: expiry still ahead. It binds `:ws` and `:acct`, nothing else. The planner
#: picks with it and the runway counts with it.
ELIGIBLE_SQL = (
    " WHERE m.workspace_id = :ws AND m.state = 'available'"
    "   AND NOT EXISTS (SELECT 1 FROM post_intents p"
    "                   WHERE p.workspace_id = m.workspace_id"
    "                     AND p.media_item_id = m.id"
    "                     AND p.ig_account_id = :acct"
    f"                    AND p.state NOT IN ({_TERMINAL_STATES_SQL}))"
    "   AND NOT EXISTS (SELECT 1 FROM post_locks l"
    "                   WHERE l.workspace_id = m.workspace_id"
    "                     AND l.media_item_id = m.id"
    "                     AND (l.expires_at IS NULL OR l.expires_at > now())"
    "                     AND (l.ig_account_id IS NULL OR l.ig_account_id = :acct))"
)


@dataclass(frozen=True)
class Pool:
    """What one account can post from right now, folder by folder."""

    #: :func:`weights` over the connected folders, in the order read.
    weights: dict[str, float]
    #: Eligible files per folder a draw can land on, and only those: a
    #: connected folder with no ratio or a ratio above 0 (0 when nothing in it
    #: is eligible). An Off folder is never counted (:func:`pool`), so it is
    #: not here, whatever it holds.
    counts: dict[str, int]
    verified_counts: Optional[dict[str, int]] = None
    exclusions: Optional[dict[str, dict[str, int]]] = None

    @property
    def drawable(self) -> list[tuple[str, float]]:
        """The folders the draw can land on, with their weights: Off folders,
        and folders with nothing eligible, weigh 0 and are not among them."""
        return [(sid, weight) for sid, weight in self.weights.items() if weight > 0]

    @property
    def eligible(self) -> int:
        """Every file the planner could pick for this account now: every
        count :attr:`counts` holds, since it holds only the folders a draw can
        land on."""
        return sum(
            (
                self.verified_counts
                if self.verified_counts is not None
                else self.counts
            ).values()
        )

    @property
    def candidates(self):
        return sum(self.counts.values())

    @property
    def candidates_after_a_mint(self):
        return self.candidates - 1

    @property
    def eligible_after_a_mint(self) -> int:
        """What a mint drawn from this pool leaves: one file fewer. A minted
        file is live for the account, so :data:`ELIGIBLE_SQL` leaves it out;
        the rest of the pool is as read, so long as nothing else in the
        mint's transaction moved it."""
        return self.eligible - 1


async def pool_folders(executor, *, workspace_id: str) -> list[dict]:
    """The folders a draw can land on, with their current ratios — `source_id`,
    `ratio`. A folder in `error` or removed is not among them; the mix is keyed
    on the source (a name is a label), so a mix row without a source_id (set
    before 071) is not joined and shapes nothing. The same for every account
    of the workspace, so a read over several accounts passes it to
    :func:`pool` once."""
    return await readers.rows(
        executor,
        "SELECT s.id AS source_id, x.ratio"
        "  FROM media_sources s"
        + _CURRENT_MIX_JOIN
        + " WHERE s.workspace_id = :ws AND s.state <> 'error'"
        "   AND " + CONNECTED_SQL,
        ws=workspace_id,
    )


async def pool(
    executor,
    *,
    workspace_id: str,
    ig_account_id: str,
    folders: Optional[list[dict]] = None,
    capability=None,
    context=None,
) -> Pool:
    """The planner's pool for one account: the folders a draw can land on
    (:func:`pool_folders`, read here unless *folders* hands that read in),
    then the eligible files in each, weighed by :func:`weights`.
    `scheduler.execute_plan_slot` draws from it and `content_runway` counts
    it, so the files drawn from and the days left cannot disagree."""
    from src.services.target import source_policy, versioned_mix

    if context is None:
        context = await source_policy.suitability_context(
            executor,
            workspace_id=workspace_id,
            ig_account_ids=[ig_account_id],
            capability=capability,
        )
    source_policy.require_context(executor, context, workspace_id)
    saved = await versioned_mix.state(executor, str(workspace_id))
    if versioned_mix.converted(saved):
        if folders is None:
            folders = await pool_folders(executor, workspace_id=workspace_id)
        query = (
            "WITH verdicts AS (SELECT false AS strict,v.* FROM ("
            + source_policy.CANDIDATE_SQL
            + ") v "
            "UNION ALL SELECT true AS strict,v.* FROM ("
            + source_policy.SUITABILITY_SQL
            + ") v),"
            "counts AS (SELECT source_id,count(*) FILTER(WHERE suitable AND NOT strict) AS n,"
            "count(*) FILTER(WHERE suitable AND strict) AS verified FROM verdicts GROUP BY source_id),"
            "reasons AS (SELECT source_id,jsonb_object_agg(reason,n) AS exclusions FROM "
            "(SELECT source_id,reason,count(*) AS n FROM verdicts WHERE NOT strict AND reason IS NOT NULL "
            "GROUP BY source_id,reason) r GROUP BY source_id) "
            "SELECT c.*,COALESCE(r.exclusions,'{}'::jsonb) AS exclusions FROM counts c LEFT JOIN reasons r USING(source_id)"
        )
        counts = await readers.rows(
            executor,
            query,
            **context.binds(),
            acct=str(ig_account_id),
            sid=None,
            media=None,
            intent=None,
        )
        have = {str(r["source_id"]): int(r["n"]) for r in counts}
        verified = {str(r["source_id"]): int(r["verified"]) for r in counts}
        exclusions = {str(r["source_id"]): r["exclusions"] for r in counts}
        targets = {
            str(r["source_id"]): Decimal(r["ratio"])
            if r["ratio"] is not None and have.get(str(r["source_id"]), 0) > 0
            else Decimal(0)
            for r in folders
        }
        shares = versioned_mix.normalize_positive(targets)
        return Pool(
            weights=shares,
            counts={sid: have.get(sid, 0) for sid in shares if targets[sid] > 0},
            verified_counts={
                sid: verified.get(sid, 0) for sid in shares if targets[sid] > 0
            },
            exclusions=exclusions,
        )
    if folders is None:
        folders = await pool_folders(executor, workspace_id=workspace_id)
    # Counted in the folders that can be drawn only, the one guard on an Off
    # folder (ratio 0): it weighs 0 whatever it holds, so its files are never
    # read and :attr:`Pool.counts` holds only these. With none of them the
    # count still runs, bound to no folder, so the read is the same statement
    # every time.
    sources = [
        str(row["source_id"])
        for row in folders
        if row["ratio"] is None or row["ratio"] > 0
    ]
    counts = await readers.rows(
        executor,
        "SELECT m.source_id, count(*) AS n FROM media_items m"
        + ELIGIBLE_SQL
        + "   AND m.source_id = ANY(CAST(:sources AS uuid[]))"
        + " GROUP BY m.source_id",
        ws=workspace_id,
        acct=ig_account_id,
        sources=sources,
    )
    have = {str(row["source_id"]): int(row["n"]) for row in counts}
    # The folder rows as read, each with its count: :func:`weights` reads the
    # source id and the ratio itself.
    shaped = [{**row, "n": have.get(str(row["source_id"]), 0)} for row in folders]
    return Pool(
        weights=weights(shaped),
        counts={source_id: have.get(source_id, 0) for source_id in sources},
    )


async def _connected(executor, *, workspace_id: str):
    """The workspace's connected folders — `id`, `label`."""
    return await readers.rows(
        executor,
        f"SELECT s.id, {_LABEL} AS label FROM media_sources s"
        f" WHERE s.workspace_id = :ws AND {CONNECTED_SQL} ORDER BY s.created_at, s.id",
        ws=str(workspace_id),
    )


async def set_mix(
    executor, *, workspace_id: str, mix: Any, by_user_id: Optional[str]
) -> list[dict]:
    """Replace the workspace's current mix: supersede every current row (the
    id-less ones from before 071 included), then insert the new ones — in the
    CALLER's transaction, so the table never shows half a mix. Every id must
    be a connected folder of THIS workspace (`unknown_source` otherwise,
    before anything is written). Returns the mix as stored."""
    await guard_legacy(executor, str(workspace_id))
    rows = normalize(mix)
    labels: dict[str, str] = {}
    if rows:
        found = await _connected(executor, workspace_id=workspace_id)
        labels = {str(r["id"]): str(r["label"]) for r in found}
        for source_id, _ in rows:
            if source_id not in labels:
                raise MixInvalid("unknown_source", source_id)
        # Something must post. A folder the save does not name is AUTOMATIC
        # and posts, so all-zero rows are refused only when they cover every
        # connected folder ("one Off, one Automatic" is a legal mix).
        if all(r == 0 for _, r in rows) and set(labels) <= {sid for sid, _ in rows}:
            raise MixInvalid("all_off")
    # One writer at a time per workspace: two admins saving at once would
    # both supersede, and the loser's inserts would hit the unique index as a
    # raw integrity error. The lock dies with the transaction.
    await executor.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"case_mix:{workspace_id}"},
    )
    await executor.execute(
        text(
            "UPDATE category_post_case_mix SET effective_to = now()"
            " WHERE workspace_id = :ws AND effective_to IS NULL"
        ),
        {"ws": str(workspace_id)},
    )
    for source_id, ratio in rows:
        await executor.execute(
            text(
                "INSERT INTO category_post_case_mix"
                " (workspace_id, source_id, category, ratio, created_by_user_id)"
                " VALUES (:ws, CAST(:source_id AS uuid), :category, :ratio,"
                "         CAST(:by AS uuid))"
            ),
            {
                "ws": str(workspace_id),
                "source_id": source_id,
                "category": labels[source_id],
                "ratio": ratio,
                "by": by_user_id,
            },
        )
    return [{"source_id": source_id, "ratio": ratio} for source_id, ratio in rows]


async def mix_view(executor, *, workspace_id: str) -> list[dict]:
    """What the card renders: every connected folder with its label, state,
    available media count, current ratio (None = automatic, 0 = Off) and the
    share of posts it gets — `effective`, a percentage from :func:`weights`
    over workspace-wide eligibility (the planner subtracts one account's own
    live intents and locks on top, so the card's number is approximate)."""
    rows = await readers.rows(
        executor,
        "SELECT s.id AS source_id, s.provider, " + _LABEL + " AS name, s.state,"
        "       (SELECT count(*) FROM media_items m"
        "         WHERE m.workspace_id = s.workspace_id AND m.source_id = s.id"
        "           AND m.state = 'available') AS media_count,"
        "       x.ratio"
        "  FROM media_sources s"
        + _CURRENT_MIX_JOIN
        + " WHERE s.workspace_id = :ws AND "
        + CONNECTED_SQL
        + " ORDER BY s.created_at, s.id",
        ws=str(workspace_id),
    )
    shaped = [
        {
            "source_id": str(r["source_id"]),
            "provider": r["provider"],
            "name": r["name"],
            "state": r["state"],
            "media_count": int(r["media_count"] or 0),
            "ratio": None if r["ratio"] is None else float(r["ratio"]),
        }
        for r in rows
    ]
    # A source in `error` (its folder is gone in Drive) is listed, with its
    # state, but weighs nothing: the planner skips it too, since nothing it
    # would draw can be fetched.
    share = weights(
        [
            {
                "source_id": r["source_id"],
                "ratio": r["ratio"],
                "n": 0 if r["state"] == "error" else r["media_count"],
            }
            for r in shaped
        ]
    )
    for r in shaped:
        r["effective"] = round(share[r["source_id"]] * 100, 1)
    return shaped


# Versioned allocation lives beside the legacy arithmetic; these are the frozen
# service ports for membership, conversion and the existing resource.
from src.services.target.versioned_mix import (  # noqa: E402, F401
    MixConflict,
    MixProtocolRequired,
    MixMembershipChange,
    MixConversionUnavailable,
    resource,
    mutate,
    convert_workspace,
    allocate_membership,
    preview,
    connect_drive,
    remove_drive,
)


async def guard_legacy(executor, ws):
    from src.services.target import source_state
    from src.services.target.versioned_mix import state, pending

    await source_state.lock_all_workspace_sources(executor, workspace_id=ws)
    if not pending(await state(executor, ws)):
        raise MixProtocolRequired()


def _source_facts(row, account_id, *, assignable, exclusions, archive):
    """One mix row as one destination sees it. *archive* is the source's
    `instagram_sync.status`, or empty for a Drive folder, which has no
    progress facts."""
    return destination_readiness.SourceFacts(
        provider=row["provider"],
        enabled=row["enabled"],
        assignable=assignable,
        state=row["state"],
        origin_here=row["origin_account_id"] == account_id,
        exclusions=exclusions,
        indexing=archive.get("indexing", False),
        partial=archive.get("partial", False),
        stalled=archive.get("stalled", False),
        needs_reconnect=archive.get("needs_reconnect", False),
    )


#: The listed destinations (`LISTED_ACCOUNT_SQL`, in the Accounts tab's order)
#: with what their readiness reads about each: whether it was added by handle
#: alone, its own `ig_login` grant (`CREDENTIAL_STATUS_SQL`), and the sources
#: whose scope admits it. A Drive folder is workspace-wide; an archive serves
#: only its origin, through an active self-assignment, which is the v1
#: `origin_only` rule `fn_source_asset_suitability` applies to each item (138).
_DESTINATIONS_SQL = (
    "SELECT a.id::text AS account_id, a.handle, a.display_name, a.state,"
    "       a.provider_account_ref LIKE 'manual:%' AS handle_only,"
    f"      {CREDENTIAL_STATUS_SQL},"
    "       ARRAY(SELECT s.id::text FROM media_sources s"
    "              WHERE s.workspace_id = :ws"
    "                AND (s.provider = 'gdrive'"
    "                     OR (s.provider = 'instagram_archive'"
    "                         AND s.origin_ig_account_id = a.id"
    "                         AND EXISTS (SELECT 1 FROM media_source_destinations d"
    "                                      WHERE d.workspace_id = :ws AND d.source_id = s.id"
    "                                        AND d.ig_account_id = a.id"
    "                                        AND d.state = 'active')))"
    "              ORDER BY s.id) AS assignable"
    "  FROM ig_accounts a"
    "  LEFT JOIN oauth_credentials c"
    "    ON c.workspace_id = a.workspace_id AND c.ig_account_id = a.id"
    "   AND c.provider = :provider"
    f" WHERE a.workspace_id = :ws AND {LISTED_ACCOUNT_SQL}"
    f" {LISTED_ACCOUNT_ORDER_SQL}"
)


async def destination_projection(executor, *, workspace_id, rows):
    """Every listed destination's readiness, supply and effective shares, for
    the mix resource.

    One trusted suitability context for the whole read. The destinations and
    their assignable sources are one statement; each archive's progress facts
    are read once (`instagram_sync.status`, the source half of the progress
    read); then :func:`pool`, the planner's own read, runs per destination.
    `destination_readiness` turns those facts into the state, tone and remedy.
    A destination whose own grant is unusable shows no supply. Configured
    targets stay intact: the effective shares are each destination's own.

    An archive whose saved checkpoint cannot be read (`status` refuses it) is
    read as stalled and partial, with no coverage, and logged by source id and
    reason alone. One malformed row must not fail the whole read: the editor
    that lets an admin act on it comes from this read.
    """
    from src.services.target import instagram_sync, source_policy, versioned_mix

    ws = str(workspace_id)
    context = await source_policy.suitability_context(executor, workspace_id=ws)
    accounts = await readers.rows(
        executor, _DESTINATIONS_SQL, ws=ws, provider=IG_LOGIN_PROVIDER
    )
    capability = executor.info.get("archive_capability")
    archives = {}
    for r in rows:
        if r["provider"] != "instagram_archive":
            continue
        try:
            archives[r["source_id"]] = await instagram_sync.status(
                executor,
                context=context,
                source_id=r["source_id"],
                capability=capability,
            )
        except instagram_sync.ArchiveCheckpointRefused as exc:
            # `status` raises this after its statements have run, so nothing
            # is left aborted. The reason is logged, never the checkpoint.
            logger.warning(
                "archive source %s: checkpoint unreadable (%s); read as stalled",
                r["source_id"],
                exc.reason,
            )
            archives[r["source_id"]] = {"stalled": True, "partial": True}
    output = []
    for account in accounts:
        drawn = await pool(
            executor,
            workspace_id=ws,
            ig_account_id=account["account_id"],
            folders=rows,
            context=context,
        )
        assignable = set(account["assignable"])
        exclusions = drawn.exclusions or {}
        facts = destination_readiness.DestinationFacts(
            account_state=account["state"],
            handle_only=bool(account["handle_only"]),
            credential_status=account["credential_status"],
            candidates=drawn.candidates,
            sources=[
                _source_facts(
                    r,
                    account["account_id"],
                    assignable=r["source_id"] in assignable,
                    exclusions=exclusions.get(r["source_id"], {}),
                    archive=archives.get(r["source_id"], {}),
                )
                for r in rows
            ],
        )
        state, tone, remedy = destination_readiness.readiness(facts)
        # Nothing can be posted to it, so it shows no supply.
        shown = not destination_readiness.own_grant_unusable(
            account["state"], account["credential_status"]
        )
        verified = (
            drawn.verified_counts if drawn.verified_counts is not None else drawn.counts
        )
        output.append(
            {
                "account_id": account["account_id"],
                "handle": account["handle"],
                "display_name": account["display_name"],
                "state": state,
                "tone": tone,
                "partial_coverage": destination_readiness.partial_coverage(facts),
                "candidates": drawn.candidates if shown else 0,
                "eligible": drawn.eligible if shown else 0,
                "leading_exclusion": destination_readiness.leading_exclusion(facts),
                "remedy": remedy,
                "sources": [
                    {
                        "source_id": r["source_id"],
                        "assignable": source.assignable,
                        "candidates": drawn.counts.get(r["source_id"], 0)
                        if shown
                        else 0,
                        "eligible": verified.get(r["source_id"], 0) if shown else 0,
                        "effective_share": versioned_mix.ratio_string(
                            drawn.weights.get(r["source_id"], Decimal(0))
                            if shown
                            else Decimal(0)
                        ),
                        "exclusions": source.exclusions,
                        "indexing": source.indexing,
                        "partial_coverage": source.partial,
                        "stalled": source.stalled,
                        "needs_reconnect": source.needs_reconnect,
                        "oldest_observed_at": archives.get(r["source_id"], {}).get(
                            "coverage_from"
                        ),
                    }
                    for r, source in zip(rows, facts.sources)
                ],
            }
        )
    return output
