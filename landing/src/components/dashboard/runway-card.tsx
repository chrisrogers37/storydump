import { Hourglass } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/dashboard/empty-state";
import { RESOLVED_IN } from "@/lib/conditions";
import type { RunwayRow } from "@/lib/runway";

/**
 * Days of content left, per account (#1478) — how long each account can keep
 * posting from what its library holds now (`deriveRunway`).
 *
 * An account below the warning level is marked, and the sentence under the
 * list says what the workspace is told and when, so the mark is never the only
 * place the rule lives. The figure is a floor: a posted file comes back once
 * its repost window passes, which this does not count ahead of time.
 */
export function RunwayCard({
  rows,
  belowDays,
}: {
  rows: RunwayRow[];
  belowDays: number;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Content left</CardTitle>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <EmptyState
            icon={Hourglass}
            title="No Instagram account connected yet"
            description="Connect one to see how many days of content it has left."
            action={{
              label: RESOLVED_IN.accounts.action,
              href: RESOLVED_IN.accounts.href,
            }}
          />
        ) : (
          <ul className="divide-y">
            {rows.map((row) => (
              <li
                key={row.key}
                className="flex flex-wrap items-center justify-between gap-3 py-2 text-sm"
              >
                <span className="font-medium">{row.name}</span>
                <span
                  className={
                    row.low ? "font-medium text-amber-700" : "text-muted-foreground"
                  }
                >
                  {row.headline}
                  {row.low ? " — running low" : ""} · {row.detail}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-3 text-xs text-muted-foreground">
          {`You are told once when an account drops below ${belowDays} days of content.`}
        </p>
      </CardContent>
    </Card>
  );
}
