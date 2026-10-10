import { cardButtons, type CardAction } from "@/components/landing/approval-card"
import { ACTION_LABELS, actionsFor } from "@/lib/intents"
import { formatCalendarDate } from "@/lib/zoned-dates"

const explain: Partial<Record<CardAction, string>> = {
  post: "Publishes it to your Story through Instagram’s official API.",
  posted: "Posted it by hand? One tap keeps the record straight.",
  skip: "Not today. It goes back in the line-up for later.",
  reject: "Not ever. It won’t come up again.",
}

/** The card's four choices with one line each; Open Instagram needs none. */
export const cardLegend = cardButtons.flatMap(({ action, label }) =>
  explain[action] ? [{ label, text: explain[action] }] : []
)

// The web Queue's buttons for a story awaiting approval, as the dashboard
// renders them, with Instagram API publishing on.
export const queueButtons = actionsFor("awaiting_approval", true).map(
  (a) => ACTION_LABELS[a]
)

/** The site's list style: "a, b and c", no serial comma. */
export function joinWithAnd(items: readonly string[]): string {
  return items.length < 2 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`
}

/** The time slot the demo cards show. */
export const DEMO_SLOT = "2026-10-02 09:00 Europe/London"

/**
 * The slot's day, as a chat heads that day's messages. Taken from the slot, so
 * the two cannot disagree: a fixed date is an example, and never "Today".
 */
export const DEMO_DAY = formatCalendarDate(DEMO_SLOT.slice(0, 10), { month: "long", day: "numeric" })
