import { NextRequest, NextResponse } from "next/server";
import { isUuid } from "@/lib/session";
import { passThrough, requireWorkspace } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * DELETE /api/workspaces/[id]/sources/[sourceId] — remove a folder from the
 * sync. The API PAUSES the source rather than deleting it (the media and
 * its history stay; picking the folder again revives it).
 */
export async function DELETE(
  _request: NextRequest,
  context: { params: Promise<{ id: string; sourceId: string }> },
) {
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;
  const { sourceId } = await context.params;
  if (!isUuid(sourceId)) {
    return NextResponse.json({ error: "invalid_source" }, { status: 400 });
  }
  const result = await targetFetch<{ source_id?: string; state?: string }>(
    `/workspaces/${id}/sources/${sourceId}`,
    token,
    { method: "DELETE" },
  );
  if (!result.ok) return passThrough(result);
  return NextResponse.json({ sourceId, state: result.data?.state ?? "paused" });
}
