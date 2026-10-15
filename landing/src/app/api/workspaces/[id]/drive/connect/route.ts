import { NextRequest, NextResponse } from "next/server";
import { refuseCrossSite, requireWorkspace } from "@/lib/route-guards";
import { proxyStartOfGrant } from "@/lib/start-proxy";
import { isGoogleAuthorizationUrl } from "@/lib/drive";

/**
 * POST /api/workspaces/[id]/drive/connect — start the WORKSPACE's Drive grant
 * (#1165): one Google grant per workspace,
 * folders picked under it.
 */
export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const refused = refuseCrossSite(request);
  if (refused) return refused;

  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;

  return proxyStartOfGrant(`/workspaces/${id}/drive/connect`, token, isGoogleAuthorizationUrl);
}
