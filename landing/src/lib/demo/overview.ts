import type { StatsResponse } from "@/lib/dashboard-payloads";
import type { Intent, IntentState } from "@/lib/intents";
import type { FinishedStory, SampleWorkspace } from "./fixtures";

/** The states a visitor's tap leaves a story in. */
const DECIDED: readonly IntentState[] = ["posted", "skipped", "rejected"];

/**
 * The Overview as the visitor has left the sample (#1649): the month's counts
 * with each story the visitor decided added in, as a real workspace's counts
 * would move, and those stories first in Recent activity.
 *
 * A story marked posted counts on today, the window's last day, and in its
 * folder's share of the mix. One that is skipped or rejected stops waiting.
 */
export function overviewOf(
  sample: Pick<SampleWorkspace, "stats" | "sources" | "history">,
  queue: Intent[],
): { stats: StatsResponse; activity: FinishedStory[] } {
  const decided = queue
    .filter((i) => DECIDED.includes(i.state))
    .map(({ id, state, file_name, category, schedule_slot_at, entered_state_at }) => ({
      id,
      state,
      file_name,
      category,
      schedule_slot_at,
      entered_state_at,
    }))
    .sort((a, b) => Date.parse(b.entered_state_at) - Date.parse(a.entered_state_at));
  const posted = decided.filter((s) => s.state === "posted");
  const ended = (state: IntentState) => decided.filter((s) => s.state === state).length;

  const { stats } = sample;
  const by = stats.intents_by_state;
  const sourceOf = new Map(sample.sources.sources.map((s) => [s.folder_name, s.id] as const));
  const postedBySource = { ...stats.posted_by_source };
  for (const story of posted) {
    const source = sourceOf.get(story.category ?? "");
    if (source) postedBySource[source] = (postedBySource[source] ?? 0) + 1;
  }
  const last = stats.posts_by_day.length - 1;

  return {
    stats: {
      ...stats,
      intents_by_state: {
        ...by,
        posted: (by.posted ?? 0) + posted.length,
        skipped: (by.skipped ?? 0) + ended("skipped"),
        rejected: (by.rejected ?? 0) + ended("rejected"),
        awaiting_approval: (by.awaiting_approval ?? 0) - decided.length,
      },
      posts_by_day: stats.posts_by_day.map((day, i) =>
        i === last ? { ...day, count: day.count + posted.length } : day,
      ),
      posted_by_source: postedBySource,
    },
    activity: [...decided, ...sample.history],
  };
}
