"""The category mix — `category_post_case_mix`, D23's Type 2 SCD table — and
the ONE service that writes it (owner ruling 2026-09-06).

A picked Drive folder's subfolders are categories (the sync tags each file
with the subfolder it sits in — `media_sync`, `google_drive_adapter`), and the
workspace weights how often each posts: memes 70 / merch 30. `scheduler.
execute_plan_slot` draws a category by these weights before it picks a file.

D23: the table keeps its row shape (one row per category per effective
period; `effective_to IS NULL` is the current row, `uq_case_mix_current`
makes two current rows for a category impossible) and **sum-to-one is
service-enforced HERE** — a deferred cross-row trigger was considered and
rejected, so this module is where the invariant lives. Setting a mix is one
supersede (close every current row) then one insert per category, in the
caller's transaction. Ratios are fractions in [0, 1] summing to 1 within a
rounding tolerance; the web converts percentages. An empty mix is legal and
means "no weighting" — the planner falls back to oldest-first over the pool.

Files directly in the picked folder have no category (`NULL`) and cannot be
weighted by name; they post only when no weighted category has media.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target import readers

#: Sum-to-one tolerance: four decimal places per row, so a three-way split
#: cannot hit 1.0000 exactly and must not be refused for it.
SUM_TOLERANCE = 0.001
MAX_CATEGORY_LEN = 100
#: More categories than this is not a mix anyone typed by hand; refused by name.
MAX_CATEGORIES = 500


class MixInvalid(StorydumpError):
    """The mix cannot be stored as sent. `reason` is one of: not_a_list ·
    empty_category · category_too_long · duplicate_category · bad_ratio ·
    sum_not_one."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(
            f"category mix invalid: {reason}" + (f" — {detail}" if detail else "")
        )


def normalize(mix: Any) -> list[tuple[str, float]]:
    """Validate and normalize `[{"category", "ratio"}, …]` to `[(name, ratio)]`
    with trimmed names and ratios rounded to the table's four places. Refuses
    by name; an empty list is legal."""
    if not isinstance(mix, list):
        raise MixInvalid("not_a_list")
    if len(mix) > MAX_CATEGORIES:
        raise MixInvalid("too_many_categories", str(len(mix)))
    rows: list[tuple[str, float]] = []
    seen: set[str] = set()
    for entry in mix:
        if not isinstance(entry, dict):
            raise MixInvalid("not_a_list", "each entry must be an object")
        name = entry.get("category")
        if not isinstance(name, str) or not name.strip():
            raise MixInvalid("empty_category")
        name = name.strip()
        if len(name) > MAX_CATEGORY_LEN:
            raise MixInvalid("category_too_long", name[:20])
        if name in seen:
            raise MixInvalid("duplicate_category", name)
        seen.add(name)
        ratio = entry.get("ratio")
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(float(ratio))
        ):
            # NaN passes every comparison and NUMERIC stores it; D23's
            # invariant would be bypassable by one JSON `NaN` (review of #1251).
            raise MixInvalid("bad_ratio", name)
        ratio = round(float(ratio), 4)
        if ratio < 0 or ratio > 1:
            raise MixInvalid("bad_ratio", name)
        rows.append((name, ratio))
    total = round(sum(r for _, r in rows), 4)
    if rows and round(abs(total - 1.0), 4) > SUM_TOLERANCE:
        raise MixInvalid("sum_not_one", f"sum is {total:.4f}")
    return rows


async def set_mix(
    executor, *, workspace_id: str, mix: Any, by_user_id: Optional[str]
) -> list[dict]:
    """Replace the workspace's current mix: supersede every current row, then
    insert the new ones — in the CALLER's transaction, so the table never
    shows half a mix. Returns the mix as stored."""
    rows = normalize(mix)
    # One writer at a time per workspace: two admins saving at once would
    # both supersede, and the loser's inserts would hit `uq_case_mix_current`
    # as a raw integrity error. The lock dies with the transaction.
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
    for name, ratio in rows:
        await executor.execute(
            text(
                "INSERT INTO category_post_case_mix"
                " (workspace_id, category, ratio, created_by_user_id)"
                " VALUES (:ws, :category, :ratio, CAST(:by AS uuid))"
            ),
            {
                "ws": str(workspace_id),
                "category": name,
                "ratio": ratio,
                "by": by_user_id,
            },
        )
    return [{"category": name, "ratio": ratio} for name, ratio in rows]


async def current_mix(executor, *, workspace_id: str) -> list[dict]:
    """The current rows, as `[{"category", "ratio"}]` — ratios as floats."""
    rows = await readers.rows(
        executor,
        "SELECT category, ratio FROM category_post_case_mix"
        " WHERE workspace_id = :ws AND effective_to IS NULL ORDER BY category",
        ws=str(workspace_id),
    )
    return [{"category": r["category"], "ratio": float(r["ratio"])} for r in rows]


async def discovered_categories(executor, *, workspace_id: str) -> list[dict]:
    """What the sync has found: each category with its count of AVAILABLE
    media, the root's uncategorized files as `category: None`. What the card
    lists, so a person weights folders that exist rather than typing names."""
    rows = await readers.rows(
        executor,
        "SELECT category, count(*) AS media_count FROM media_items"
        " WHERE workspace_id = :ws AND state = 'available'"
        " GROUP BY category ORDER BY category NULLS LAST",
        ws=str(workspace_id),
    )
    return [
        {"category": r["category"], "media_count": int(r["media_count"])} for r in rows
    ]
