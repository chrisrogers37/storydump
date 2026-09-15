import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isUuid, isWorkspaceId } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";

/**
 * DELETE /api/workspaces/[id]/tokens/[tokenId] — revoke one of the
 * workspace's service identities. Admin floor is the API's.
 */
export async function DELETE(
  _request: NextRequest,
  context: { params: Promise<{ id: string; tokenId: string }> },
) {
  const token = await getSessionToken();
  if (!token)
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  const { id, tokenId } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }
  if (!isUuid(tokenId)) {
    return NextResponse.json({ error: "invalid_token" }, { status: 400 });
  }
  const result = await targetFetch<{ revoked?: unknown }>(
    `/workspaces/${id}/tokens/${tokenId}`,
    token,
    { method: "DELETE" },
  );
  if (!result.ok) {
    return NextResponse.json(
      { error: result.error },
      { status: result.status },
    );
  }
  if (result.data?.revoked !== true) {
    // The API confirms a revoke with `revoked: true` and nothing else; any
    // other answer is malformed, decided here like the mint routes do.
    return NextResponse.json({ error: "malformed_response" }, { status: 502 });
  }
  return NextResponse.json({ tokenId, revoked: true });
}
