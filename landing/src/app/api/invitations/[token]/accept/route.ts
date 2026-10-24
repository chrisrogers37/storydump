import { NextRequest, NextResponse } from "next/server";
import {
  INVITE_COOKIE,
  WORKSPACE_COOKIE,
  WORKSPACE_COOKIE_OPTIONS,
  isWorkspaceId,
} from "@/lib/session";
import { passThrough, refuseCrossSite, requireSessionToken } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * POST /api/invitations/[token]/accept
 *
 * The invite token is never sent anywhere but the router, which holds only its
 * SHA256 (`workspace_invitations.token_hash`). This route does not log it and
 * puts it in no error body, and the router masks it in its access log and in
 * its own log lines (`src/utils/logger.py`). An invite token is a bearer
 * credential for joining a workspace, and the URL it arrives in is still the
 * weakest part of that: this route's own path, like `/join/[token]`'s, can
 * reach the hosting platform's request log, which no code here controls.
 *
 * Accepting is `fn_invitation_accept`, a SECURITY DEFINER door: it has to write
 * a `workspace_members` row for a user who is by definition not yet a member,
 * which is exactly the privilege crossing a door exists for. There is no
 * version of this the BFF can do itself.
 */
export async function POST(
  request: NextRequest,
  context: { params: Promise<{ token: string }> },
) {
  const refused = refuseCrossSite(request);
  if (refused) return refused;

  const sessionToken = await requireSessionToken();
  if (sessionToken instanceof NextResponse) return sessionToken;

  const { token } = await context.params;
  if (!token || token.length > 256) {
    return NextResponse.json({ error: "invalid_invitation" }, { status: 400 });
  }

  // The token rides the PATH, not the body. Encoded because it is attacker-
  // supplied and lands in a URL; the API holds only its SHA256 either way.
  const result = await targetFetch<{ workspace_id: string }>(
    `/invitations/${encodeURIComponent(token)}/accept`,
    sessionToken,
    { method: "POST" },
  );

  if (!result.ok) return passThrough(result);

  const response = NextResponse.json({ workspaceId: result.data.workspace_id });

  // Spent. Left behind it would bounce the next sign-in back to an invitation
  // that has already been accepted.
  response.cookies.delete(INVITE_COOKIE);

  // Land them in the workspace they just joined. Guarded because this value
  // becomes a path segment on the next request.
  if (isWorkspaceId(result.data.workspace_id)) {
    response.cookies.set(
      WORKSPACE_COOKIE,
      result.data.workspace_id,
      WORKSPACE_COOKIE_OPTIONS,
    );
  }
  return response;
}
