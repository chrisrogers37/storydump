import { Card, CardContent, StatCard } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * A skeleton is a PROMISE about what is about to appear (#1060).
 *
 * ── It follows the components it stands in for ──────────────────────────────
 *
 * The pool row is two cards (#1060): `derivePoolHealth` returns
 * `posted_multiple` and `eligible_for_posting` as `null` unconditionally,
 * because they have no target-side source (#1048), so their cards do not
 * exist. The rest follows `PoolHealth` and `MediaGrid` (#1051): the
 * withheld-figures note and the category filter row are reserved, and grid
 * cards use `h-32` and `gap-3`.
 *
 * ── The rule this file follows ──────────────────────────────────────────────
 *
 * A FIXED-COUNT region must match exactly. An UNKNOWN-LENGTH region gets a
 * representative count and says so. The pool row is fixed at two and is pinned
 * by `media-loading-contract.test.tsx`; the media grid and the filter chips
 * stand in for lists whose length is not knowable before the fetch returns, so
 * a count there is a placeholder rather than a claim.
 *
 * That distinction is the whole point: over-reserving a fixed region is the
 * #1060 defect — the same class as #1050, a UI element promising something
 * the system will not honour.
 */
export default function MediaLoading() {
  return (
    <div className="space-y-6">
      {/* PoolHealth: two cards, then the withheld-figures note. */}
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-4">
          {Array.from({ length: 2 }).map((_, i) => (
            <StatCard
              key={i}
              label={<Skeleton className="h-4 w-28" />}
              value={<Skeleton className="h-9 w-14" />}
              detail={<Skeleton className="my-0.5 h-3 w-20" />}
            />
          ))}
        </div>
        {/*
          Reserved on every load, not occasionally: both figures are `null`
          unconditionally, so this note always renders. Two lines is what it
          wraps to at desktop width.
        */}
        <div className="space-y-1">
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-2/3" />
        </div>
      </div>

      {/* MediaGrid: filter chips, the grid, then the bounded-list note. */}
      <div className="space-y-4">
        {/* `size="sm"` buttons are h-8. Count is representative — the real row
            is "All" plus one chip per category, which the fetch decides. */}
        <div className="flex flex-wrap gap-2">
          {/* Literal class strings, never `w-${n}` — Tailwind cannot see a
              constructed class name, so a computed width is purged and the
              chip renders at zero width. */}
          <Skeleton className="h-8 w-16 rounded-md" />
          <Skeleton className="h-8 w-20 rounded-md" />
          <Skeleton className="h-8 w-20 rounded-md" />
          <Skeleton className="h-8 w-24 rounded-md" />
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <Card key={i} className="overflow-hidden">
              {/* The real card's media area is a fixed h-32 panel, not a
                  square: an aspect-square placeholder would over-reserve ~150px
                  a row and drop the grid when the images arrive. */}
              <Skeleton className="h-32 w-full rounded-none" />
              <CardContent className="space-y-2 p-3">
                <Skeleton className="h-5 w-3/4" />
                <div className="flex items-center justify-between">
                  <Skeleton className="h-5 w-20 rounded-md" />
                  <Skeleton className="h-5 w-16 rounded-md" />
                </div>
                <div className="flex items-center justify-between">
                  <Skeleton className="h-3 w-12" />
                  <Skeleton className="h-3 w-20" />
                </div>
                {/* Link…, Schedule… and Auto-approve: `size="sm"` buttons (h-8)
                    at the row's end, wrapping on a narrow card as the real row does. */}
                <div className="flex flex-wrap justify-end gap-2">
                  <Skeleton className="h-8 w-16 rounded-md" />
                  <Skeleton className="h-8 w-24 rounded-md" />
                  <Skeleton className="h-8 w-28 rounded-md" />
                </div>
              </CardContent>
            </Card>
          ))}
        </div>

        <Skeleton className="mt-2 h-3 w-1/2" />
      </div>
    </div>
  );
}
