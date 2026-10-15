import { requireWorkspacePage } from "@/lib/page-guards";
import { workspaceFetch } from "@/lib/workspaces";
import {
  derivePoolHealth,
  type AccountsResponse,
  type MediaResponse,
  type StatsResponse,
  type WorkspaceConfig,
} from "@/lib/dashboard-payloads";
import { pastPostsOf } from "@/lib/archive-media";
import { parseSourceMix } from "@/lib/source-mix";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { PoolHealth } from "@/components/dashboard/media/pool-health";
import { MediaGrid } from "@/components/dashboard/media/media-grid";

/**
 * The library is a bounded read and the bound is stated.
 *
 * The pool COUNTS come from `stats`, never from this list — an aggregate
 * derived from a truncated set is a confident wrong number, which is the whole
 * reason the aggregate route exists.
 */
const MEDIA_LIMIT = 100;

export default async function MediaLibraryPage() {
  const { workspaceId } = await requireWorkspacePage();

  const [mediaResult, statsResult, accountsResult, configResult] = await Promise.all([
    workspaceFetch<MediaResponse>(
      `media?state=available&limit=${MEDIA_LIMIT}`,
      workspaceId,
    ),
    workspaceFetch<StatsResponse>("stats", workspaceId),
    workspaceFetch<AccountsResponse>("accounts", workspaceId),
    workspaceFetch<WorkspaceConfig>("", workspaceId),
  ]);

  // Both, for the same reason the overview gates on all of its own: a pool
  // header over an empty grid states a fact about the library we did not
  // establish.
  if (!mediaResult.ok || !statsResult.ok) {
    return <RouterUnavailable what="Your media library" />;
  }

  const items = mediaResult.data.media ?? [];
  const health = derivePoolHealth(statsResult.data);

  // Scheduling needs the accounts and the workspace's zone, but the library
  // does not: without them the grid still shows, and the Schedule dialog says
  // they could not be read rather than offering a picker it cannot fill.
  const targets =
    accountsResult.ok && configResult.ok
      ? { accounts: accountsResult.data.accounts ?? [], workspaceTz: configResult.data.tz }
      : null;

  // An empty library tells a workspace with past posts as a source that they are
  // on or Off, not to connect Drive. Nothing else on the page uses the mix, so
  // it is read for an empty library alone, after the reads the page stands on.
  // Only a converted workspace's mix (protocol 2) has one, and a mix that could
  // not be read says nothing: the library says to connect Drive, as it does for
  // protocol 1.
  const mixResult =
    items.length === 0 ? await workspaceFetch<unknown>("category-mix", workspaceId) : null;
  const pastPosts = mixResult?.ok ? pastPostsOf(parseSourceMix(mixResult.data)) : null;

  return (
    <div className="space-y-6">
      <PoolHealth health={health} />

      <MediaGrid
        items={items}
        limit={MEDIA_LIMIT}
        workspaceId={workspaceId}
        targets={targets}
        // For Auto-approve's tooltip. Unread is null, not off: the tooltip
        // then says nothing about posting rather than something untrue.
        apiPublishing={configResult.ok ? configResult.data.api_publishing_enabled : null}
        pastPosts={pastPosts}
      />
    </div>
  );
}
