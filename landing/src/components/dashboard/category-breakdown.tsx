"use client";

import { Layers } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/dashboard/empty-state";

/**
 * Category mix, from `stats.media_by_category` + `posted_by_category` (#1044).
 *
 * ── The configured mix is MISSING, and the card says so ────────────────────
 *
 * This card's whole subject was drift: actual share against the configured
 * share, coloured green/yellow/red by the gap. The configured mix has no target
 * route (#1048 — it lives only in the legacy core service), so the comparison
 * cannot be drawn and neither can the colour.
 *
 * What it must NOT do is keep the layout and quietly render actual-only, or
 * default the configured share to zero. Either would show a drift figure that
 * is not a drift figure — the same class of defect as a header over three empty
 * panels. So the comparison is withdrawn EXPLICITLY: the actual share is shown
 * because it is real, and one line states what is missing and why. When Chris
 * rules on #1048 this either grows the comparison back or loses the note.
 *
 * `configured_ratio` is `number | null` rather than optional so that a future
 * edit cannot reintroduce the silent version with `?? 0` — the compiler stops
 * at this file until it says what it shows.
 */
interface CategoryView {
  category: string;
  posted: number;
  total: number;
  actual_ratio: number;
  configured_ratio: number | null;
}

export function CategoryBreakdown({ categories }: { categories: CategoryView[] }) {
  const anyConfigured = categories.some((c) => c.configured_ratio !== null);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Category Mix</CardTitle>
      </CardHeader>
      <CardContent>
        {categories.length === 0 ? (
          <EmptyState
            icon={Layers}
            title="No category data yet"
            description="Category mix appears after your first posts."
          />
        ) : (
          <div className="space-y-4">
            {categories.map((cat) => (
              <div key={cat.category} className="space-y-1.5">
                <div className="flex items-center justify-between text-sm">
                  <span className="font-medium capitalize">{cat.category}</span>
                  <span className="text-muted-foreground">
                    {cat.posted}/{cat.total} posted
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full rounded-full bg-primary transition-all"
                      style={{
                        width: `${Math.min(cat.actual_ratio * 100, 100)}%`,
                      }}
                    />
                  </div>
                  <span className="font-mono text-xs text-muted-foreground">
                    {(cat.actual_ratio * 100).toFixed(0)}%
                    {cat.configured_ratio !== null && (
                      <span>/{(cat.configured_ratio * 100).toFixed(0)}%</span>
                    )}
                  </span>
                </div>
              </div>
            ))}

            {!anyConfigured && (
              <p className="border-t pt-3 text-xs text-muted-foreground">
                Showing the actual share of posts per category. The configured
                target mix is not available from this API yet, so the
                configured-versus-actual comparison this card used to draw is
                withheld rather than estimated.
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
