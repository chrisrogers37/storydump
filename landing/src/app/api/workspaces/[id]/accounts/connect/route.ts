import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId } from "@/lib/session";
import { proxyStartOfGrant } from "@/lib/start-proxy";
import { isInstagramAuthorizationUrl } from "@/lib/destination";

/**
 * POST /api/workspaces/[id]/accounts/connect — start the Instagram grant that
 * ADDS a destination (owner ruling 2026-09-04): no account is named, the
 * callback lands the signed-in Instagram account on the row it already has
 * here or on a new one. The per-destination sibling connects an existing row.
 */
export async function POST(
  _request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  const { id } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }

  return proxyStartOfGrant(`/workspaces/${id}/accounts/connect`, token, isInstagramAuthorizationUrl);
}
