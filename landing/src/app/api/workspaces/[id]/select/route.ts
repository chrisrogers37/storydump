import { NextRequest, NextResponse } from "next/server";
import { WORKSPACE_COOKIE, WORKSPACE_COOKIE_OPTIONS } from "@/lib/session";
import { passThrough, requireWorkspace } from "@/lib/route-guards";
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
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;

  const result = await targetFetch(`/workspaces/${id}`, token);
  if (!result.ok) return passThrough(result);

  const response = NextResponse.json({ ok: true });
  response.cookies.set(WORKSPACE_COOKIE, id, WORKSPACE_COOKIE_OPTIONS);
  return response;
}
