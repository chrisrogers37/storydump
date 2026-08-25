import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * A skeleton is a PROMISE about what is about to appear (#1060).
 *
 * ── The filed defect was one symptom of a staler condition ──────────────────
 *
 * #1060 reports four pool-health placeholders resolving into two cards. True,
 * and the two missing cards are not slow — they do not exist: `derivePoolHealth`
 * returns `posted_multiple` and `eligible_for_posting` as `null`
 * unconditionally, because #1048 left them with no target-side source.
 *
 * But measuring the rest of the skeleton against the rest of the page turned up
 * three more mismatches, which is enough to stop listing symptoms and name what
 * they share: THIS FILE WAS WRITTEN AGAINST AN EARLIER PAGE AND WAS NEVER
 * RE-DERIVED. #1051 rewrote both `PoolHealth` and `MediaGrid`; nothing
 * re-checked the skeleton that stands in for them, because nothing can — no
 * gate compares the two. Measured at 1440px before this change:
 *
 *   - pool row promised 4 cards, page renders 2
 *   - the withheld-figures note was not reserved at all
 *   - the category filter row was not reserved at all (~52px of shift)
 *   - grid cards used `aspect-square` images and `gap-4`; the page uses
 *     `h-32` and `gap-3` — roughly 150px of over-reservation per row
 *
 * So it is re-derived from the components as they are now, not patched.
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
 * #1060 defect — the same class as the Upload control removed in #1050, a UI
 * element promising something the system will not honour.
 */
export default function MediaLoading() {
  return (
    <div className="space-y-6">
      {/* PoolHealth: two cards, then the withheld-figures note. */}
      <div className="space-y-3">
        <div className="grid gap-4 sm:grid-cols-2">
          {Array.from({ length: 2 }).map((_, i) => (
            <Card key={i}>
              <CardHeader className="pb-2">
                <Skeleton className="h-4 w-28" />
              </CardHeader>
              <CardContent>
                <Skeleton className="h-8 w-14" />
                <Skeleton className="h-3 w-20 mt-2" />
              </CardContent>
            </Card>
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
                  square: an aspect-square placeholder over-reserved ~150px a
                  row and dropped the grid when the images arrived. */}
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
              </CardContent>
            </Card>
          ))}
        </div>

        <Skeleton className="mt-2 h-3 w-1/2" />
      </div>
    </div>
  );
}
