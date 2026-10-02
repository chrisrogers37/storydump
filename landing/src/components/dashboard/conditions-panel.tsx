import Link from "next/link";
import { AlertTriangle, CheckCircle2, Circle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ALL_CLEAR_DETAIL,
  SETUP_STEP_COUNT,
  type Condition,
  type SetupStep,
} from "@/lib/conditions";

/**
 * The overview's condition surface: what needs the workspace's attention,
 * each line linking to the tab that resolves it (`deriveConditions`).
 *
 * THE ALL-CLEAR IS A SENTENCE, NOT AN EMPTY LIST. An empty panel looks exactly
 * like one that failed to load, so "nothing needs your attention" is said in
 * words, with what was checked. The page renders this only from reads that
 * answered; a read that failed is its unavailable state, never an all-clear.
 *
 * NOR IS AN EMPTY WORKSPACE ALL CLEAR. With no Instagram account or no Drive
 * folder there is nothing to go wrong, so the all-clear's place goes to the
 * first missing setup step (`nextSetupStep`). A condition still wins: it is
 * about something that exists and is broken.
 */
export function ConditionsPanel({
  conditions,
  setupStep = null,
}: {
  conditions: Condition[];
  setupStep?: SetupStep | null;
}) {
  if (conditions.length === 0 && setupStep) {
    return (
      <Card className="py-4">
        <CardContent className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-start gap-3">
            <Circle
              className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
            <div>
              <p className="text-xs text-muted-foreground">
                Setup · step {setupStep.number} of {SETUP_STEP_COUNT}
              </p>
              <p className="font-medium">{setupStep.title}</p>
              <p className="text-sm text-muted-foreground">{setupStep.detail}</p>
            </div>
          </div>
          {/* ml-8 lines it up under the text when it wraps on a phone. */}
          <Button size="sm" className="ml-8" asChild>
            <Link href={setupStep.href}>{setupStep.action}</Link>
          </Button>
        </CardContent>
      </Card>
    );
  }

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
