import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * Pool health, as far as `stats` can answer it (#1044).
 *
 * ── Two of the four cards are withdrawn, not zeroed ────────────────────────
 *
 * "Reuse Rate" needed `posted_multiple` and "Eligible for Posting" needed a
 * TTL-aware count. Neither has a target-side source (#1048), and both were
 * RATES — which is what makes defaulting them to 0 worse than dropping them.
 * `posted_multiple / total` with a zero numerator renders "0%" and reads as a
 * measured finding: nothing is being reused. That is a claim about the
 * workspace, made from the absence of a column.
 *
 * So the two cards are gone and one line says why. The remaining two are
 * counted, not derived, and are true.
 */
interface PoolHealthView {
  total_active: number;
  never_posted: number;
  by_category: { name: string; count: number }[];
  posted_once: number | null;
  posted_multiple: number | null;
  eligible_for_posting: number | null;
}

export function PoolHealth({ health }: { health: PoolHealthView }) {
  const total = health.total_active || 1;
  const withheld = [
    health.posted_multiple === null ? "reuse rate" : null,
    health.eligible_for_posting === null ? "eligible-to-post count" : null,
  ].filter(Boolean);

  return (
    <div className="space-y-3">
      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total Active
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{health.total_active}</div>
            <p className="mt-1 text-xs text-muted-foreground">
              {health.by_category.length} categories
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Never Posted
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{health.never_posted}</div>
            <p className="mt-1 text-xs text-muted-foreground">
              {Math.round((health.never_posted / total) * 100)}% untouched
            </p>
          </CardContent>
        </Card>
      </div>

      {withheld.length > 0 && (
        <p className="text-xs text-muted-foreground">
          The {withheld.join(" and the ")} are not available from this API yet
          and are withheld rather than shown as zero — a zero here would read as
          a measurement of your library rather than a missing column.
        </p>
      )}
    </div>
  );
}
