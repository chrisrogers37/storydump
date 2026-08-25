import { NextRequest, NextResponse } from "next/server";
import {
  WORKSPACE_COOKIE,
  WORKSPACE_COOKIE_OPTIONS,
  getSessionToken,
  isWorkspaceId,
} from "@/lib/session";
import { targetFetch } from "@/lib/target-api";
import { INVITE_COOKIE } from "@/app/join/[token]/start/route";

/**
 * POST /api/invitations/[token]/accept
 *
 * The invite token is never sent anywhere but the router, which holds only its
 * SHA256 (`workspace_invitations.token_hash`). It is not logged and does not
 * appear in an error body — an invite token is a bearer credential for joining
 * a workspace, and the URL it arrives in is already the weakest part of that.
 *
 * Accepting is `fn_invitation_accept`, a SECURITY DEFINER door: it has to write
 * a `workspace_members` row for a user who is by definition not yet a member,
 * which is exactly the privilege crossing a door exists for. There is no
 * version of this the BFF can do itself.
 */
export async function POST(
  _request: NextRequest,
  context: { params: Promise<{ token: string }> },
) {
  const sessionToken = await getSessionToken();
  if (!sessionToken) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const { token } = await context.params;
  if (!token || token.length > 256) {
    return NextResponse.json({ error: "invalid_invitation" }, { status: 400 });
  }

  const result = await targetFetch<{ workspace_id: string }>(
    "/invitations/accept",
    sessionToken,
    { method: "POST", body: JSON.stringify({ token }) },
  );

  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }

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
