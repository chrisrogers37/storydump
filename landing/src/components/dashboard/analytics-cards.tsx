import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * The headline counts, from `stats.intents_by_state` (#1044).
 *
 * ── Why the method split is gone rather than defaulted ─────────────────────
 *
 * The legacy cards carried `ig_*` and `telegram_*` fields, added in 2026-06 to
 * fix #466: the old "Success Rate" lumped Instagram publish attempts together
 * with Telegram delivery attempts, so a 958-row delivery burst (#467) rendered
 * as a 1% success rate while every real Instagram post had succeeded. The
 * component then fell back to the lumped aggregates when the split was absent.
 *
 * That fallback would be actively harmful here. `stats` serves no split — so
 * the fallback would fire every time, silently reproducing the exact figure
 * #466 was raised to kill, under a label still saying "Instagram".
 *
 * It is not needed either, and that is the better reason to delete it: the
 * divisor is drawn from `intents_by_state`, and Telegram delivery is NOT an
 * intent state. There is nothing of the other kind in the data to lump. So the
 * cards name what they now count — publish outcomes — and the bug is excluded
 * by construction rather than by a field that has to keep arriving.
 */
interface SummaryView {
  posted: number;
  skipped: number;
  rejected: number;
  failed: number;
  total: number;
  success_rate: number | null;
  avg_per_day: number | null;
}

/**
 * What a card shows when the figure would have to be invented.
 *
 * The same rule the rest of this dashboard already follows — `pool-health`
 * omits its withheld cards and says why, `general-tab` renders "Not available
 * yet" in place of a control. This is the in-place form, because these two
 * absences are TEMPORARY: a rate with no publish attempts behind it is not a
 * missing column, it is a workspace that has not published yet, and it starts
 * reporting on its own the moment one attempt happens. Dropping the card each
 * time would make the grid change shape as data arrives.
 *
 * The reason is printed rather than left blank: "—" alone says a figure is
 * missing but not that nothing is wrong.
 */
const WITHHELD = "—";

export function AnalyticsCards({ summary }: { summary: SummaryView }) {
  const cards = [
    {
      title: "Posts published",
      value: summary.posted,
      detail:
        summary.avg_per_day === null
          ? "No posting window yet"
          : `${summary.avg_per_day.toFixed(1)}/day avg`,
    },
    {
      title: "Publish success rate",
      // A rate over an empty divisor is not a low rate. `0%` here reads as a
      // verdict on the workspace, and it is the one figure on this screen a
      // person would act on.
      value:
        summary.success_rate === null
          ? WITHHELD
          : `${(summary.success_rate * 100).toFixed(0)}%`,
      detail:
        summary.success_rate === null
          ? "No publish attempts yet"
          : summary.failed > 0
            ? `${summary.posted} posted / ${summary.failed} failed`
            : `${summary.posted} posted`,
    },
    {
      title: "Skipped",
      value: summary.skipped,
      detail: summary.rejected > 0 ? `${summary.rejected} rejected` : "not posted",
    },
    {
      title: "Failed",
      value: summary.failed,
      detail: summary.failed > 0 ? "publish attempts" : "none",
    },
  ];

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      {cards.map((card) => (
        <Card key={card.title}>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              {card.title}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-semibold tracking-tight">{card.value}</p>
            <p className="mt-1 text-xs text-muted-foreground">{card.detail}</p>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
