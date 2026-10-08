import { CalendarClock } from "lucide-react";
import { requireWorkspacePage } from "@/lib/page-guards";
import { workspaceFetch } from "@/lib/workspaces";
import type { WorkspaceConfig } from "@/lib/dashboard-payloads";
import {
  LIST_LIMIT_MAX,
  NON_TERMINAL_STATES,
  queueOriginFilter,
  type IntentsResponse,
} from "@/lib/intents";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { EmptyState } from "@/components/dashboard/empty-state";
import { QueueFilter } from "@/components/dashboard/queue/queue-filter";
import { QueueList } from "@/components/dashboard/queue/queue-list";
import { QueueHeader } from "@/components/dashboard/page-headers";

/**
 * `01` H5: every list is bounded. This asks for the API's ceiling; the
 * response echoes the limit it actually applied, and a queue that reaches it
 * says so rather than rendering the first page as the whole.
 */
const QUEUE_LIMIT = LIST_LIMIT_MAX;

/**
 * The act-on-it surface (#1033): every intent the ledger has not closed, in
 * slot order, with the matrix's human levers on the one state that has them.
 *
 * Read from the ledger, never from a queue table of its own — the intent row
 * IS the record (`02` §4), and this page is its non-terminal view. Two
 * reads, both guarded: a workspace config that could not be fetched would
 * mean rendering slots in the wrong clock and Approve on a guess, and the
 * rule for a page with N dependencies is to guard on all N.
 *
 * `?origin=planned` narrows it to the stories a person planned (#1413). The
 * API applies the filter, so the view stays exact past the page limit.
 */
export default async function QueuePage({
  searchParams,
}: {
  searchParams: Promise<{ origin?: string }>;
}) {
  const { workspaceId } = await requireWorkspacePage();
  const origin = queueOriginFilter((await searchParams).origin);

  const [configResult, intentsResult] = await Promise.all([
    workspaceFetch<WorkspaceConfig>("", workspaceId),
    workspaceFetch<IntentsResponse>(
      `intents?state=${NON_TERMINAL_STATES.join(",")}&limit=${QUEUE_LIMIT}` +
        (origin ? `&origin=${origin}` : ""),
      workspaceId,
    ),
  ]);

  if (!configResult.ok || !intentsResult.ok) {
    return <RouterUnavailable what="The queue" />;
  }

  const config = configResult.data;
  const { intents, limit } = intentsResult.data;
  const tz = config.tz ?? "UTC";

  return (
    <div className="space-y-6">
      <QueueHeader tz={tz} />

      <QueueFilter origin={origin} />

      {origin === "planned" && intents.length === 0 ? (
        <EmptyState
          icon={CalendarClock}
          title="Nothing is planned"
          description="Schedule a story from the Media library to post it at a time you pick."
          action={{ label: "Open the Media library", href: "/dashboard/media" }}
        />
      ) : (
        <QueueList
          workspaceId={workspaceId}
          intents={intents}
          tz={tz}
          apiPublishingEnabled={config.api_publishing_enabled === true}
          truncatedAt={intents.length >= limit ? limit : null}
        />
      )}
    </div>
  );
}
