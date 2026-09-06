import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";

/**
 * PUT /api/workspaces/[id]/category-mix — replace how often each category
 * posts (owner ruling 2026-09-06). A resource, not a command word: the mix is
 * a table of rows the closed vocabulary has no name for. The API refuses a
 * malformed mix by name (`invalid_mix:<reason>`); this forwards the shape.
 */
export async function PUT(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  const { id } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return NextResponse.json({ error: "malformed_body" }, { status: 400 });
  }
  const mix = (raw as { mix?: unknown })?.mix;
  if (!Array.isArray(mix)) {
    return NextResponse.json({ error: "invalid_mix:not_a_list" }, { status: 400 });
  }
  const result = await targetFetch<{ mix?: unknown[] }>(`/workspaces/${id}/category-mix`, token, {
    method: "PUT",
    body: JSON.stringify({ mix }),
  });
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }
  return NextResponse.json({ mix: Array.isArray(result.data?.mix) ? result.data.mix : [] });
}
