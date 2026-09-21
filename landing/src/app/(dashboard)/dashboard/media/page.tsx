import { requireWorkspacePage } from "@/lib/page-guards";
import { workspaceFetch } from "@/lib/workspaces";
import {
  derivePoolHealth,
  type MediaResponse,
  type StatsResponse,
} from "@/lib/dashboard-payloads";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { PoolHealth } from "@/components/dashboard/media/pool-health";
import { MediaGrid } from "@/components/dashboard/media/media-grid";

/**
 * The library is a bounded read and the bound is stated (`01` H5).
 *
 * The pool COUNTS come from `stats`, never from this list — an aggregate
 * derived from a truncated set is a confident wrong number, which is the whole
 * reason the aggregate route exists.
 */
const MEDIA_LIMIT = 100;

export default async function MediaLibraryPage() {
  const { workspaceId } = await requireWorkspacePage();

  const [mediaResult, statsResult] = await Promise.all([
    workspaceFetch<MediaResponse>(
      `media?state=available&limit=${MEDIA_LIMIT}`,
      workspaceId,
    ),
    workspaceFetch<StatsResponse>("stats", workspaceId),
  ]);

  // Both, for the same reason the overview gates on all of its own: a pool
  // header over an empty grid states a fact about the library we did not
  // establish.
  if (!mediaResult.ok || !statsResult.ok) {
    return <RouterUnavailable what="Your media library" />;
  }

  const items = mediaResult.data.media ?? [];
  const health = derivePoolHealth(statsResult.data);

  return (
    <div className="space-y-6">
      <PoolHealth health={health} />

      <MediaGrid items={items} limit={MEDIA_LIMIT} />
    </div>
  );
}
