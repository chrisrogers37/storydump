import { NextRequest, NextResponse } from "next/server";
import { passThrough, requireWorkspace } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * GET /api/workspaces/[id]/drive — the workspace's Google Drive grant:
 * presence and freshness, never a token (`GET /workspaces/{ws}/drive`).
 */
export async function GET(_request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;
  const result = await targetFetch<{ drive?: unknown }>(`/workspaces/${id}/drive`, token);
  if (!result.ok) return passThrough(result);
  return NextResponse.json({ drive: result.data?.drive ?? null });
}
