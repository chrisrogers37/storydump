import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { getWorkspaceConfig, workspaceFetch } from "@/lib/workspaces";
import { NON_TERMINAL_STATES, type IntentsResponse } from "@/lib/intents";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { QueueList } from "@/components/dashboard/queue/queue-list";

/**
 * `01` H5: every list is bounded. This asks for the API's ceiling
 * (`LIST_LIMIT_MAX`); the response echoes the limit it actually applied, and
 * a queue that reaches it says so rather than rendering the first page as
 * the whole.
 */
const QUEUE_LIMIT = 200;

/**
 * The act-on-it surface (#1033): every intent the ledger has not closed, in
 * slot order, with the matrix's human levers on the one state that has them.
 *
 * Read from the ledger, never from a queue table of its own — the intent row
 * IS the record (`02` §4), and this page is its non-terminal view. Two
 * reads, both guarded: a workspace config that could not be fetched would
 * mean rendering slots in the wrong clock and Approve on a guess, and the
 * rule for a page with N dependencies is to guard on all N.
 */
export default async function QueuePage() {
  const session = await getSession().catch(() => null);
  if (!session) redirect("/login");
  // Middleware already required a selected workspace to reach any route under
  // /dashboard. Repeated because a page is reachable in tests and in a direct
  // render without it, and `activeWorkspaceId!` would be a non-null assertion
  // on a value that is legitimately null for every brand-new user.
  const workspaceId = session.activeWorkspaceId;
  if (!workspaceId) redirect("/welcome");

  const [configResult, intentsResult] = await Promise.all([
    getWorkspaceConfig(workspaceId),
    workspaceFetch<IntentsResponse>(
      `intents?state=${NON_TERMINAL_STATES.join(",")}&limit=${QUEUE_LIMIT}`,
      workspaceId,
    ),
  ]);

  if (!configResult.ok || !intentsResult.ok) {
    return <RouterUnavailable what="The queue" />;
  }

  const config = configResult.data;
  const { intents, limit } = intentsResult.data;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Queue</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Every post that is not done yet, in slot order. Times are in {config.tz}.
        </p>
      </div>

      <QueueList
        workspaceId={workspaceId}
        intents={intents}
        tz={config.tz}
        apiPublishingEnabled={config.api_publishing_enabled}
        truncatedAt={intents.length >= limit ? limit : null}
      />
    </div>
  );
}
