import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";

/**
 * GET /api/workspaces/[id]/drive — the workspace's Google Drive grant:
 * presence and freshness, never a token (`GET /workspaces/{ws}/drive`).
 */
export async function GET(_request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  const { id } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }
  const result = await targetFetch<{ drive?: unknown }>(`/workspaces/${id}/drive`, token);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }
  return NextResponse.json({ drive: result.data?.drive ?? null });
}
