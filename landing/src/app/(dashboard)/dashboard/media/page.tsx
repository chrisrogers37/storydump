import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { workspaceFetch } from "@/lib/workspaces";
import type { MediaLibraryResponse } from "@/lib/dashboard-payloads";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { PoolHealth } from "@/components/dashboard/media/pool-health";
import { MediaGrid } from "@/components/dashboard/media/media-grid";
import { MediaUploadWrapper } from "@/components/dashboard/media/media-upload-wrapper";

export default async function MediaLibraryPage() {
  const session = await getSession().catch(() => null);
  if (!session) redirect("/login");
  // Middleware already required a selected workspace to reach any route under
  // /dashboard. Repeated because a page is reachable in tests and in a direct
  // render without it, and `activeWorkspaceId!` would be a non-null assertion
  // on a value that is legitimately null for every brand-new user.
  const workspaceId = session.activeWorkspaceId;
  if (!workspaceId) redirect("/welcome");
  const result = await workspaceFetch<MediaLibraryResponse>(
    "media-library?page=1&page_size=20",
    workspaceId,
  );
  if (!result.ok) return <RouterUnavailable what="Your media library" />;

  const library = result.data;

  const poolHealth = library?.pool_health ?? {
    total_active: 0,
    never_posted: 0,
    posted_once: 0,
    posted_multiple: 0,
    eligible_for_posting: 0,
    by_category: [],
  };

  return (
    <div className="space-y-6">
      <PoolHealth health={poolHealth} />

      <div className="grid gap-6 lg:grid-cols-[1fr_300px]">
        <MediaGrid initialData={library ?? {
          items: [],
          total: 0,
          page: 1,
          page_size: 20,
          categories: [],
          pool_health: poolHealth,
        }} />
        <MediaUploadWrapper categories={library?.categories ?? []} />
      </div>
    </div>
  );
}
