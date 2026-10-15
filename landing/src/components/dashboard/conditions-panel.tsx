import Link from "next/link";
import { AlertTriangle, CheckCircle2, Circle, CircleDashed } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TONE_CLASS } from "@/components/dashboard/tone";
import {
  ALL_CLEAR_DETAIL,
  SETUP_STEP_COUNT,
  type Condition,
  type SetupStep,
} from "@/lib/conditions";
import type { ReadinessView } from "@/lib/readiness";

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
 *
 * A CONVERTED WORKSPACE IS GUIDED BY READINESS instead (`readiness.ts`,
 * #1644): each account the server says is not ready yet, with its
 * badge and remedy, and an intentional all-inactive or all-Off workspace said
 * as a choice. It is per account and about things that exist, so it stands
 * beside the conditions rather than behind them, and only its absence leaves
 * the all-clear.
 */
export function ConditionsPanel({
  conditions,
  setupStep = null,
  readiness = null,
}: {
  conditions: Condition[];
  setupStep?: SetupStep | null;
  readiness?: ReadinessView | null;
}) {
  if (readiness) {
    return conditions.length === 0 ? (
      readinessCard(readiness)
    ) : (
      <>
        {conditionsCard(conditions)}
        {readinessCard(readiness)}
      </>
    );
  }

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
          {setupStep.href && (
            <Button size="sm" className="ml-8" asChild>
              <Link href={setupStep.href}>{setupStep.action}</Link>
            </Button>
          )}
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

  return conditionsCard(conditions);
}

/** The conditions, each linking to where it is resolved. A function, not a
 *  component, so the panel's returned tree is the card itself. */
function conditionsCard(conditions: Condition[]) {
  return (
    <Card className="border-amber-300">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
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

/**
 * The readiness guidance: neutral, not amber, since setting up is not a
 * fault. The server's counts head it ("1 of 2 accounts ready"); each line
 * names the account, its state and partial-coverage badges, what is true, and
 * the remedy, as a link for an admin or an ask for a member.
 */
function readinessCard(view: ReadinessView) {
  const Icon = view.intentional ? CircleDashed : Circle;
  return (
    <Card className="py-4">
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-start gap-3">
            <Icon className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <div className="min-w-0">
              {!view.intentional && <p className="text-xs text-muted-foreground">Setup</p>}
              <p className="font-medium">{view.title}</p>
              {view.detail && <p className="text-sm text-muted-foreground">{view.detail}</p>}
            </div>
          </div>
          {view.action && (
            <Button variant="outline" size="sm" className="ml-8" asChild>
              <Link href={view.action.href}>{view.action.label}</Link>
            </Button>
          )}
        </div>
        {view.lines.length > 0 && (
          <ul className="divide-y border-t">
            {view.lines.map((line) => (
              <li
                key={line.key}
                className="flex flex-wrap items-start justify-between gap-3 py-2 text-sm"
              >
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="break-all font-medium">{line.name}</span>
                    {line.badges.map((badge) => (
                      <Badge key={badge.label} variant="secondary" className={TONE_CLASS[badge.tone]}>
                        {badge.label}
                      </Badge>
                    ))}
                  </div>
                  <p className="break-words text-muted-foreground">{line.detail}</p>
                  {line.coverage && <p className="text-xs text-muted-foreground">{line.coverage}</p>}
                  {line.ask && <p className="text-xs text-muted-foreground">{line.ask}</p>}
                </div>
                {line.action && (
                  // A label carries the handle, so it may wrap on a phone.
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-auto max-w-full whitespace-normal py-1.5 text-left"
                    asChild
                  >
                    <Link href={line.action.href}>{line.action.label}</Link>
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
