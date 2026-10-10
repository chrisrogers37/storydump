import { HISTORY_STATES, type StatsResponse } from "@/lib/dashboard-payloads";
import type { Intent } from "@/lib/intents";
import { countFinished, type FinishedStory, type SampleWorkspace } from "./fixtures";

/** The states Recent activity lists, the dashboard's own set. */
const FINISHED = HISTORY_STATES.split(",");

/**
 * The Overview as the visitor has left the sample (#1649): the month, counted
 * again with each story the visitor decided, so its counts move as a real
 * workspace's would, and those stories first in Recent activity.
 */
export function overviewOf(
  sample: Pick<SampleWorkspace, "stats" | "history">,
  queue: Intent[],
): { stats: StatsResponse; activity: FinishedStory[] } {
  const decided = queue
    .filter((i) => FINISHED.includes(i.state))
    .sort((a, b) => Date.parse(b.entered_state_at) - Date.parse(a.entered_state_at));
  const activity: FinishedStory[] = [...decided, ...sample.history];

  const { stats } = sample;
  const { posts_by_day, posted_by_source, ...ended } = countFinished(
    activity,
    stats.posts_by_day.map((day) => day.local_date),
  );

  return {
    stats: {
      ...stats,
      intents_by_state: {
        ...stats.intents_by_state,
        ...ended,
        awaiting_approval: stats.intents_by_state.awaiting_approval - decided.length,
      },
      posts_by_day,
      posted_by_source,
    },
    activity,
  };
}
