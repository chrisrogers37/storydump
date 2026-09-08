"""The posting mix — `category_post_case_mix`, D23's Type 2 SCD table — keyed
on the CONNECTED FOLDER (owner ruling 2026-09-08: sources are the groups),
and the ONE service that writes it.

A workspace connects folders (`media_sources`); everything inside a connected
folder syncs, at any depth (the walk of #1256); and the workspace weights how
often each connected folder posts: memes 70 / merch 30. To weight two
subfolders separately, connect each as its own folder. `scheduler.
execute_plan_slot` draws a folder by these weights before it picks a file —
through :func:`weights`, the ONE function that also feeds the card's
"Posts about" column, so the two can never disagree.

D23: the table keeps its row shape (one row per source per effective period;
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

import math
from typing import Any, Optional

from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target import readers
from src.services.target.workspaces import CONNECTED_SQL

#: Sum-to-one tolerance: four decimal places per row, so a three-way split
#: cannot hit 1.0000 exactly and must not be refused for it.
SUM_TOLERANCE = 0.001
#: More rows than this is not a mix anyone typed by hand; refused by name.
MAX_SOURCES = 500

#: The label a source row shows — its folder name, else its ref. Every query
#: here aliases `media_sources` as `s`, as `CONNECTED_SQL` expects.
_LABEL = "COALESCE(s.config->>'folder_name', s.config->>'folder_ref', 'folder')"


class MixInvalid(StorydumpError):
    """The mix cannot be stored as sent. `reason` is one of: not_a_list ·
    empty_source · duplicate_source · bad_ratio · sum_not_one · all_off ·
    too_many_sources · unknown_source · ambiguous_name."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"mix invalid: {reason}" + (f" — {detail}" if detail else ""))


def _ratio(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        # NaN passes every comparison and NUMERIC stores it; D23's invariant
        # would be bypassable by one JSON `NaN` (review of #1251).
        raise MixInvalid("bad_ratio", label)
    ratio = round(float(value), 4)
    if ratio < 0 or ratio > 1:
        raise MixInvalid("bad_ratio", label)
    return ratio


def _sum_to_one(rows: list[tuple[str, float]]) -> None:
    positives = [r for _, r in rows if r > 0]
    if rows and not positives:
        raise MixInvalid("all_off")
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
    the FINAL split (F4 as re-locked 2026-09-07): with explicit weights
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
    n_auto = sum(n for _, n in auto)
    if explicit and auto:
        total_ratio = sum(r for _, r, _ in explicit)
        r_min = min(r / total_ratio for _, r, _ in explicit)
        share_auto = n_auto / (n_auto + sum(n for _, _, n in explicit))
        pool = min(share_auto, r_min / (1 + r_min))
    elif auto:
        pool = 1.0
    else:
        pool = 0.0
    for source_id, n in auto:
        out[source_id] = pool * n / n_auto
    if explicit:
        total_ratio = sum(r for _, r, _ in explicit)
        for source_id, ratio, _ in explicit:
            out[source_id] = (1 - pool) * ratio / total_ratio
    return out


async def _connected(executor, *, workspace_id: str, ids: Optional[list[str]] = None):
    """The workspace's connected folders — `id`, `label` — all of them, or
    the named ones (for a save's validation)."""
    sql = (
        f"SELECT s.id, {_LABEL} AS label FROM media_sources s"
        f" WHERE s.workspace_id = :ws AND {CONNECTED_SQL}"
    )
    params: dict[str, Any] = {"ws": str(workspace_id)}
    if ids is not None:
        sql += " AND CAST(s.id AS text) = ANY(CAST(:ids AS text[]))"
        params["ids"] = list(ids)
    return await readers.rows(executor, sql + " ORDER BY s.created_at, s.id", **params)


async def set_mix(
    executor, *, workspace_id: str, mix: Any, by_user_id: Optional[str]
) -> list[dict]:
    """Replace the workspace's current mix: supersede every current row (the
    id-less ones from before 071 included), then insert the new ones — in the
    CALLER's transaction, so the table never shows half a mix. Every id must
    be a connected folder of THIS workspace (`unknown_source` otherwise,
    before anything is written). Returns the mix as stored."""
    rows = normalize(mix)
    labels: dict[str, str] = {}
    if rows:
        found = await _connected(
            executor, workspace_id=workspace_id, ids=[sid for sid, _ in rows]
        )
        labels = {str(r["id"]): str(r["label"]) for r in found}
        for source_id, _ in rows:
            if source_id not in labels:
                raise MixInvalid("unknown_source", source_id)
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
        "  LEFT JOIN category_post_case_mix x"
        "    ON x.workspace_id = s.workspace_id AND x.source_id = s.id"
        "   AND x.effective_to IS NULL"
        " WHERE s.workspace_id = :ws AND "
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
    share = weights(
        [
            {"source_id": r["source_id"], "ratio": r["ratio"], "n": r["media_count"]}
            for r in shaped
        ]
    )
    for r in shaped:
        r["effective"] = round(share[r["source_id"]] * 100, 1)
    return shaped


# ---- v1 compat — the card deployed before this phase (delete with the v1
# keys; nothing else here depends on it) ----------------------------------


def v1_shape(rows: list[dict]) -> dict:
    """The keys the old card reads (`mix` by name, `categories` with counts),
    derived from the view for ONE release so the API can deploy ahead of the
    web without the Settings page breaking."""
    return {
        "mix": [
            {"category": r["name"], "ratio": r["ratio"]}
            for r in rows
            if r["ratio"] is not None and r["ratio"] > 0
        ],
        "categories": [
            {"category": r["name"], "media_count": r["media_count"]} for r in rows
        ],
    }


def resolve_names(rows: list[dict], mix: Any) -> list[dict]:
    """The old card's `PUT` body — `[{"category", "ratio"}]` by folder name —
    turned into rows by source, from the view (which carries name and id). A
    name that is two connected folders is `ambiguous_name`; one that is none
    is `unknown_source`."""
    if not isinstance(mix, list):
        raise MixInvalid("not_a_list")
    by_name: dict[str, list[str]] = {}
    for r in rows:
        by_name.setdefault(str(r["name"]), []).append(str(r["source_id"]))
    out: list[dict] = []
    for entry in mix:
        if not isinstance(entry, dict):
            raise MixInvalid("not_a_list", "each entry must be an object")
        name = entry.get("category")
        ids = by_name.get(name.strip()) if isinstance(name, str) else None
        if not ids:
            raise MixInvalid("unknown_source", str(name)[:40])
        if len(ids) > 1:
            raise MixInvalid("ambiguous_name", str(name)[:40])
        out.append({"source_id": ids[0], "ratio": entry.get("ratio")})
    return out
