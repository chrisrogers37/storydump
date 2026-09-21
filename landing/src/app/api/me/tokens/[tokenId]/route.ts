import { NextRequest, NextResponse } from "next/server";
import { isUuid } from "@/lib/session";
import { passThrough, requireSessionToken } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * DELETE /api/me/tokens/[tokenId] — revoke one of the person's own tokens.
 * The API answers 404 for a token that is not the caller's, and this tier
 * passes that through as it is: a revoke is a resource verb, never a
 * command (`sources/[sourceId]/route.ts` is the analogue).
 */
export async function DELETE(
  _request: NextRequest,
  context: { params: Promise<{ tokenId: string }> },
) {
  const token = await requireSessionToken();
  if (token instanceof NextResponse) return token;
  const { tokenId } = await context.params;
  if (!isUuid(tokenId)) {
    return NextResponse.json({ error: "invalid_token" }, { status: 400 });
  }
  const result = await targetFetch<{ revoked?: unknown }>(
    `/me/tokens/${tokenId}`,
    token,
    {
      method: "DELETE",
    },
  );
  if (!result.ok) return passThrough(result);
  // Confirmed only when the API confirmed it. `revoked: true` is the API's
  // contract; anything else is passed through as "not confirmed" rather than
  // promoted to a success the row may not reflect.
  if (result.data?.revoked !== true) {
    // The API confirms a revoke with `revoked: true` and nothing else; any
    // other answer is malformed, decided here like the mint routes do.
    return NextResponse.json({ error: "malformed_response" }, { status: 502 });
  }
  return NextResponse.json({ tokenId, revoked: true });
}
