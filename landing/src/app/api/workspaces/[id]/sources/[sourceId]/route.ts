import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isUuid, isWorkspaceId } from "@/lib/session";
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
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  const { id, sourceId } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }
  if (!isUuid(sourceId)) {
    return NextResponse.json({ error: "invalid_source" }, { status: 400 });
  }
  const result = await targetFetch<{ source_id?: string; state?: string }>(
    `/workspaces/${id}/sources/${sourceId}`,
    token,
    { method: "DELETE" },
  );
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }
  return NextResponse.json({ sourceId, state: result.data?.state ?? "paused" });
}
