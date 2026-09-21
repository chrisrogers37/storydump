import { NextRequest, NextResponse } from "next/server";
import { passThrough, requireWorkspace } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/** A Drive id: what the API accepts as `parent`, checked here only for shape. */
const FOLDER_ID = /^[A-Za-z0-9_-]{1,128}$/;

/**
 * GET /api/workspaces/[id]/drive/folders?parent=… — the folders under
 * `parent` (the Drive root when absent), read through the workspace's grant.
 * The API owns every refusal (`drive_not_connected`, `drive_unavailable`);
 * this only keeps a malformed parent from travelling.
 */
export async function GET(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;
  const parent = request.nextUrl.searchParams.get("parent");
  if (parent !== null && !FOLDER_ID.test(parent)) {
    return NextResponse.json({ error: "invalid_parent" }, { status: 400 });
  }
  const query = parent ? `?parent=${encodeURIComponent(parent)}` : "";
  const result = await targetFetch<{ parent?: string; folders?: unknown[] }>(
    `/workspaces/${id}/drive/folders${query}`,
    token,
  );
  if (!result.ok) return passThrough(result);
  return NextResponse.json({
    parent: result.data?.parent ?? "root",
    folders: Array.isArray(result.data?.folders) ? result.data.folders : [],
  });
}
