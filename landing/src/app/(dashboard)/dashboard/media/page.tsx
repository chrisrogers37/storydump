import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { workspaceFetch } from "@/lib/workspaces";
import {
  derivePoolHealth,
  type MediaResponse,
  type StatsResponse,
} from "@/lib/dashboard-payloads";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { PoolHealth } from "@/components/dashboard/media/pool-health";
import { MediaGrid } from "@/components/dashboard/media/media-grid";
import { MediaUploadWrapper } from "@/components/dashboard/media/media-upload-wrapper";

/**
 * The library is a bounded read and the bound is stated (`01` H5).
 *
 * The pool COUNTS come from `stats`, never from this list — an aggregate
 * derived from a truncated set is a confident wrong number, which is the whole
 * reason the aggregate route exists.
 */
const MEDIA_LIMIT = 100;

export default async function MediaLibraryPage() {
  const session = await getSession().catch(() => null);
  if (!session) redirect("/login");
  // Middleware already required a selected workspace to reach any route under
  // /dashboard. Repeated because a page is reachable in tests and in a direct
  // render without it, and `activeWorkspaceId!` would be a non-null assertion
  // on a value that is legitimately null for every brand-new user.
  const workspaceId = session.activeWorkspaceId;
  if (!workspaceId) redirect("/welcome");

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

      <div className="grid gap-6 lg:grid-cols-[1fr_300px]">
        <MediaGrid items={items} limit={MEDIA_LIMIT} />
        <MediaUploadWrapper
          categories={health.by_category.map((c) => c.name)}
        />
      </div>
    </div>
  );
}
