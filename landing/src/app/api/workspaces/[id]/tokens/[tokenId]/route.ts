import { NextRequest, NextResponse } from "next/server";
import { isUuid } from "@/lib/session";
import { passThrough, requireWorkspace } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * DELETE /api/workspaces/[id]/tokens/[tokenId] — revoke one of the
 * workspace's service identities. Admin floor is the API's.
 */
export async function DELETE(
  _request: NextRequest,
  context: { params: Promise<{ id: string; tokenId: string }> },
) {
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;
  const { tokenId } = await context.params;
  if (!isUuid(tokenId)) {
    return NextResponse.json({ error: "invalid_token" }, { status: 400 });
  }
  const result = await targetFetch<{ revoked?: unknown }>(
    `/workspaces/${id}/tokens/${tokenId}`,
    token,
    { method: "DELETE" },
  );
  if (!result.ok) return passThrough(result);
  if (result.data?.revoked !== true) {
    // The API confirms a revoke with `revoked: true` and nothing else; any
    // other answer is malformed, decided here like the mint routes do.
    return NextResponse.json({ error: "malformed_response" }, { status: 502 });
  }
  return NextResponse.json({ tokenId, revoked: true });
}
