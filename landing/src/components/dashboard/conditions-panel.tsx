import Link from "next/link";
import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ALL_CLEAR_DETAIL, type Condition } from "@/lib/conditions";

/**
 * The overview's condition surface: what needs the workspace's attention,
 * each line linking to the tab that resolves it (`deriveConditions`).
 *
 * THE ALL-CLEAR IS A SENTENCE, NOT AN EMPTY LIST. An empty panel looks exactly
 * like one that failed to load, so "nothing needs your attention" is said in
 * words, with what was checked. The page renders this only from reads that
 * answered; a read that failed is its unavailable state, never an all-clear.
 */
export function ConditionsPanel({ conditions }: { conditions: Condition[] }) {
  if (conditions.length === 0) {
    return (
      <Card className="py-4">
        <CardContent className="flex items-start gap-3">
          <CheckCircle2
            className="mt-0.5 h-5 w-5 shrink-0 text-green-600"
            aria-hidden="true"
          />
          <div>
            <p className="font-medium">Nothing needs your attention</p>
            <p className="text-sm text-muted-foreground">{ALL_CLEAR_DETAIL}</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-amber-300">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <AlertTriangle className="h-5 w-5 text-amber-600" aria-hidden="true" />
          Needs your attention
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="divide-y">
          {conditions.map((condition) => (
            <li
              key={condition.key}
              className="flex flex-wrap items-center justify-between gap-3 py-2 text-sm"
            >
              <span>{condition.text}</span>
              <Button variant="outline" size="sm" asChild>
                <Link href={condition.href}>{condition.action}</Link>
              </Button>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
