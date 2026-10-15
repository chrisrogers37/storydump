import { NextRequest, NextResponse } from "next/server";
import { isUuid } from "@/lib/session";
import { passThrough, requireWorkspace } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * GET /api/workspaces/[id]/connection-results/[resultId] — what one
 * Instagram connect's callback applied (#1644), for the person and
 * session that started it, for fifteen minutes. The API answers its house 404
 * for anyone else, any other session, an expired result and no result at all,
 * and that passes through unchanged; so does the answer.
 *
 * The Settings page reads the same resource on the server when the callback
 * lands it on `?connection=<id>`; this is the browser's door to it.
 */
export async function GET(
  _request: NextRequest,
  context: { params: Promise<{ id: string; resultId: string }> },
) {
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { resultId } = await context.params;
  if (!isUuid(resultId)) {
    return NextResponse.json({ error: "invalid_result" }, { status: 400 });
  }
  const result = await targetFetch<Record<string, unknown>>(
    `/workspaces/${guard.id}/connection-results/${resultId}`,
    guard.token,
    { resourceMetadata: true },
  );
  if (!result.ok) return passThrough(result);
  return NextResponse.json(result.data);
}
