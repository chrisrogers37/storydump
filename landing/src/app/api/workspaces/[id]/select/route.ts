import { NextRequest, NextResponse } from "next/server";
import {
  WORKSPACE_COOKIE,
  WORKSPACE_COOKIE_OPTIONS,
  getSessionToken,
  isWorkspaceId,
} from "@/lib/session";
import { targetFetch } from "@/lib/target-api";

/**
 * POST /api/workspaces/[id]/select — point this browser at a workspace.
 *
 * Membership is CHECKED HERE, and that is not the security boundary — it is a
 * courtesy so a stale link produces a clean 403 instead of a dashboard that
 * renders and then fails on every panel. The real boundary is that every
 * subsequent call re-authorizes server-side; the cookie this sets grants
 * nothing on its own.
 */
export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  const { id } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }

  const result = await targetFetch(`/workspaces/${id}`, token);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }

  const response = NextResponse.json({ ok: true });
  response.cookies.set(WORKSPACE_COOKIE, id, WORKSPACE_COOKIE_OPTIONS);
  return response;
}
