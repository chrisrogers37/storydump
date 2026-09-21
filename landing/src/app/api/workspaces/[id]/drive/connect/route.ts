import { NextRequest, NextResponse } from "next/server";
import { requireWorkspace } from "@/lib/route-guards";
import { proxyStartOfGrant } from "@/lib/start-proxy";
import { isGoogleAuthorizationUrl } from "@/lib/drive";

/**
 * POST /api/workspaces/[id]/drive/connect — start the WORKSPACE's Drive grant
 * (owner ruling 2026-09-05, #1165 lean (b)): one Google grant per workspace,
 * folders picked under it. The per-folder sibling this replaces is gone.
 */
export async function POST(_request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;

  return proxyStartOfGrant(`/workspaces/${id}/drive/connect`, token, isGoogleAuthorizationUrl);
}
