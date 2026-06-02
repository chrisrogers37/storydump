import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface AnalyticsSummary {
  // Legacy lump-everything-together fields (kept for back-compat;
  // the new cards below use the method-split fields instead).
  total_posts: number;
  posted: number;
  skipped: number;
  rejected: number;
  failed: number;
  success_rate: number;
  avg_per_day: number;
  // Method-split — published 2026-06 to fix #466. The legacy
  // "Success Rate" lumped Instagram publish attempts together with
  // Telegram delivery attempts, so a 958-row Telegram delivery burst
  // (see #467 postmortem) showed up as a 1% success rate on the
  // dashboard even though every actual Instagram post had succeeded.
  ig_posted?: number;
  ig_failed?: number;
  ig_success_rate?: number;
  telegram_skipped?: number;
  telegram_failed?: number;
}

export function AnalyticsCards({ summary }: { summary: AnalyticsSummary }) {
  // Prefer method-split values; fall back to the legacy aggregates if
  // the backend hasn't been updated yet (older deployments). This keeps
  // the dashboard rendering during a partial deploy window.
  const igPosted = summary.ig_posted ?? summary.posted;
  const igFailed = summary.ig_failed ?? 0;
  const igRate = summary.ig_success_rate ?? summary.success_rate;
  const tgSkipped = summary.telegram_skipped ?? summary.skipped;
  const tgFailed = summary.telegram_failed ?? summary.failed;

  const cards = [
    {
      title: "Posts published",
      value: igPosted,
      detail: `${summary.avg_per_day.toFixed(1)}/day avg`,
    },
    {
      title: "Instagram success rate",
      value: `${(igRate * 100).toFixed(0)}%`,
      detail:
        igFailed > 0
          ? `${igPosted} posted / ${igFailed} failed`
          : `${igPosted} posted`,
    },
    {
      title: "Cards skipped",
      value: tgSkipped,
      detail: "last 30 days",
    },
    {
      title: "Delivery issues",
      value: tgFailed,
      detail:
        tgFailed > 0
          ? "telegram_manual.failed — last 30 days"
          : "last 30 days",
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
            <div className="text-2xl font-bold">{card.value}</div>
            <p className="text-xs text-muted-foreground mt-1">{card.detail}</p>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
