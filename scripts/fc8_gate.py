"""The FC-8 zero-row gate — M.1's window-prep halt (`04` L87, epic #790).

    live local/upload-origin `media_items` = 0, and `posting_history` rows
    resolving to local/upload-origin media = 0. Nonzero on either count halts
    window prep and routes to the owner with the counts.

Standalone stdlib-plus-psycopg2 module (the `dispatch-overdue.py` precedent, as
used by `migration_runner`), so the gate is unit-testable and can be run against
a branch or production read replica without importing the application.

**It reads legacy tables only and writes nothing.** That is what makes it
runnable today: the target schema does not exist yet (`src/models/target` is an
empty `Base`; zero target tables in any shipped migration), so M.1's transform
INSERTs cannot be authored against real columns — but this gate never touches
them, so it is buildable and testable now rather than behind F.2.

## Why it reports a full census instead of only the two named counts

FC-8 names `local` and `upload`. The legacy corpus carries a fourth value,
`instagram_backfill`, and the plan classifies it nowhere. Meanwhile the target's
media-source CHECK is closed at exactly one provider (`02` §2:
``CHECK (provider IN ('gdrive'))``), so on the printed schema an
`instagram_backfill` row has no destination either — yet the gate as specified
would not count it, and window prep would proceed.

**This module does not resolve that.** Deciding it is an owner ruling, not a
gate author's. What it does instead is refuse to let the question stay
invisible: it HALTS on exactly the two counts FC-8 names, and separately
DISCLOSES every other `source_type` present with its own counts, flagged
`UNCLASSIFIED`. A source value nobody has ruled on therefore shows up in the
output as an unruled value, rather than being silently folded into the safe side
by a `NOT IN ('local','upload')` that reads as deliberate.

The same applies to liveness. `media_items.is_active` is nullable, so "live"
has three states, not two. Rows with `is_active IS NULL` are counted and
reported in their own bucket rather than being assigned to either side.

## Exit codes

    0  CLEAR      — both named counts are zero
    1  HALT       — at least one named count is nonzero (window prep stops)
    2  ERROR      — the gate could not answer (missing table, bad DSN)

`UNCLASSIFIED` findings do **not** halt on their own: this gate's authority is
the two counts FC-8 gives it, and inventing a third halt condition would be the
policy invention it exists to avoid. They are printed, counted, and carried in
the JSON so a reviewer sees them.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

import psycopg2

# FC-8's named set, verbatim from `04` L87. Not widened here.
NAMED_ORIGINS = ("local", "upload")

CLEAR, HALT, ERROR = 0, 1, 2


@dataclass
class Census:
    """What the gate found. Named counts decide; the rest is disclosure."""

    live_named: int = 0
    history_named: int = 0
    by_source: dict = field(default_factory=dict)  # source_type -> counts
    unclassified: list = field(default_factory=list)
    null_active: int = 0

    @property
    def halts(self) -> bool:
        return self.live_named > 0 or self.history_named > 0

    def to_json(self) -> dict:
        return {
            "verdict": "HALT" if self.halts else "CLEAR",
            "named_origins": list(NAMED_ORIGINS),
            "live_local_upload_media_items": self.live_named,
            "posting_history_rows_on_local_upload_media": self.history_named,
            "media_items_by_source_type": self.by_source,
            "unclassified_source_types": self.unclassified,
            "media_items_with_null_is_active": self.null_active,
        }


def _rows(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def census(conn) -> Census:
    """Compute both FC-8 counts plus the disclosure census."""
    c = Census()

    # Count 1 — live media_items whose origin is one FC-8 names.
    # `is_active IS NOT FALSE` would fold NULL into "live" silently; NULL is
    # counted separately below instead, and this predicate stays exact.
    c.live_named = _rows(
        conn,
        "SELECT count(*) FROM media_items"
        " WHERE is_active IS TRUE AND source_type = ANY(%s)",
        (list(NAMED_ORIGINS),),
    )[0][0]

    # Count 2 — history rows RESOLVING to such media. The resolution is the FK
    # join, not a column on posting_history: a history row carries no origin of
    # its own, so "resolving to" can only be answered through media_items.
    # Deliberately NOT filtered on is_active: a posted row whose media was
    # since deactivated still has no target destination for its media.
    c.history_named = _rows(
        conn,
        "SELECT count(*) FROM posting_history ph"
        " JOIN media_items mi ON mi.id = ph.media_item_id"
        " WHERE mi.source_type = ANY(%s)",
        (list(NAMED_ORIGINS),),
    )[0][0]

    for source_type, live, total in _rows(
        conn,
        "SELECT source_type, count(*) FILTER (WHERE is_active IS TRUE), count(*)"
        " FROM media_items GROUP BY source_type ORDER BY source_type",
    ):
        c.by_source[source_type] = {"live": live, "total": total}
        if source_type not in NAMED_ORIGINS and source_type != "google_drive":
            c.unclassified.append(source_type)

    c.null_active = _rows(
        conn, "SELECT count(*) FROM media_items WHERE is_active IS NULL"
    )[0][0]
    return c


def render(c: Census) -> str:
    out = [
        "FC-8 zero-row gate",
        "=" * 62,
        f"  live local/upload media_items          {c.live_named}",
        f"  posting_history on local/upload media  {c.history_named}",
        "",
        f"  VERDICT: {'HALT — window prep stops' if c.halts else 'CLEAR'}",
    ]
    if c.halts:
        out += [
            "",
            "  Routes to the owner with the counts. Resolutions per `04` L87:",
            "  migrate the files to Drive first, or an explicit accept-loss",
            "  list. Both are zero-schema; weakening the `02` NOT NULL chain is",
            "  recorded as rejected.",
        ]
    out += ["", "  media_items by source_type (disclosure, not a verdict):"]
    for st, n in sorted(c.by_source.items()):
        mark = "  <-- UNCLASSIFIED by the plan" if st in c.unclassified else ""
        out.append(f"    {st:22} live={n['live']:<6} total={n['total']}{mark}")
    if c.unclassified:
        out += [
            "",
            "  UNCLASSIFIED source types are present. FC-8 names only"
            f" {'/'.join(NAMED_ORIGINS)},",
            "  and the target's media-source CHECK is closed at 'gdrive' (`02` §2), so",
            "  these rows may have no destination while not being counted above. This",
            "  gate does NOT halt on them — that would be a policy it has no authority",
            "  to make. It is an owner ruling. Reported so it cannot stay invisible.",
        ]
    if c.null_active:
        out += [
            "",
            f"  {c.null_active} media_items have is_active IS NULL — neither live"
            " nor dead.",
            "  Excluded from the live count above; reported rather than assigned.",
        ]
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dsn", required=True, help="legacy database DSN (read-only)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = p.parse_args(argv)

    try:
        conn = psycopg2.connect(args.dsn)
    except Exception as e:  # noqa: BLE001
        print(f"FC-8 gate ERROR: cannot connect: {type(e).__name__}", file=sys.stderr)
        return ERROR
    try:
        c = census(conn)
    except psycopg2.Error as e:
        # A missing table means the gate could not ANSWER. That is not "clear".
        print(f"FC-8 gate ERROR: {type(e).__name__}: {e.pgcode}", file=sys.stderr)
        return ERROR
    finally:
        conn.close()

    print(json.dumps(c.to_json(), indent=2) if args.json else render(c))
    return HALT if c.halts else CLEAR


if __name__ == "__main__":
    sys.exit(main())
