import { NextRequest, NextResponse } from "next/server";
import { refuseCrossSite, requireWorkspace } from "@/lib/route-guards";
import { proxyStartOfGrant } from "@/lib/start-proxy";
import { isInstagramAuthorizationUrl } from "@/lib/destination";

/**
 * POST /api/workspaces/[id]/accounts/connect — start the Instagram grant that
 * ADDS a destination (owner ruling 2026-09-04): no account is named, the
 * callback lands the signed-in Instagram account on the row it already has
 * here or on a new one. The per-destination sibling connects an existing row.
 */
export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  const refused = refuseCrossSite(request);
  if (refused) return refused;

  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;

  return proxyStartOfGrant(`/workspaces/${id}/accounts/connect`, token, isInstagramAuthorizationUrl);
}
