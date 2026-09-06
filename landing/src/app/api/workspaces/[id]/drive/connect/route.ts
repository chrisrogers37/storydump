import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId } from "@/lib/session";
import { proxyStartOfGrant } from "@/lib/start-proxy";
import { isGoogleAuthorizationUrl } from "@/lib/drive";

/**
 * POST /api/workspaces/[id]/drive/connect — start the WORKSPACE's Drive grant
 * (owner ruling 2026-09-05, #1165 lean (b)): one Google grant per workspace,
 * folders picked under it. The per-folder sibling this replaces is gone.
 */
export async function POST(_request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  const { id } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }

  return proxyStartOfGrant(`/workspaces/${id}/drive/connect`, token, isGoogleAuthorizationUrl);
}
