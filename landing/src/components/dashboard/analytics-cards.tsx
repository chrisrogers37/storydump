import { StatCard } from "@/components/ui/card";
import type { SummaryView } from "@/lib/dashboard-payloads";

/**
 * The headline counts, from `stats.intents_by_state` (#1044).
 *
 * ── Why there is no method split, and no fallback for one ──────────────────
 *
 * A rate that lumps Instagram publish attempts together with Telegram delivery
 * attempts misreports: a burst of deliveries (#467) reads as a near-zero
 * success rate while every real Instagram post succeeds (#466). A fallback to
 * the lumped aggregates when a split is absent would be actively harmful here:
 * `stats` serves no split, so it would fire every time, reproducing the lumped
 * figure (#466) under a label saying "Instagram".
 *
 * It is not needed either, and that is the better reason to have none: the
 * divisor is drawn from `intents_by_state`, and Telegram delivery is NOT an
 * intent state. There is nothing of the other kind in the data to lump. So the
 * cards name what they count — publish outcomes — and the lumping is excluded
 * by construction rather than by a field that has to keep arriving.
 */

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
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
      {cards.map((card) => (
        <StatCard
          key={card.title}
          label={card.title}
          value={card.value}
          detail={card.detail}
        />
      ))}
    </div>
  );
}
