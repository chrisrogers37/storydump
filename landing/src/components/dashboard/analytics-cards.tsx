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
  success_rate: number;
  avg_per_day: number;
}

export function AnalyticsCards({ summary }: { summary: SummaryView }) {
  const cards = [
    {
      title: "Posts published",
      value: summary.posted,
      detail: `${summary.avg_per_day.toFixed(1)}/day avg`,
    },
    {
      title: "Publish success rate",
      value: `${(summary.success_rate * 100).toFixed(0)}%`,
      detail:
        summary.failed > 0
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
