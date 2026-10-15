import { NextRequest, NextResponse } from "next/server";
import {
  passThrough,
  readJsonBody,
  refuseCrossSite,
  requireWorkspace,
} from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * POST /api/workspaces/[id]/category-mix/preview — what a mix action WOULD
 * make, without making it (`POST /workspaces/{ws}/category-mix/preview`). The
 * body is the PUT's action body; the answer is the would-be mix, whole, with
 * the revision unchanged. The card shows it before a change that resets every
 * share to equal, and makes the change only on Confirm.
 *
 * Forwarded like the PUT beside it: the guards check transport, session and
 * shape only, the action and its revision go through untouched, and a refusal
 * keeps its reason, status and revision. A preview writes nothing, but it is
 * still a POST that answers about the workspace, so it opens with the same
 * cross-site refusal as every other state-changing route here.
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
  const parsedBody = await readJsonBody(request);
  if (parsedBody instanceof NextResponse) return parsedBody;
  if (
    parsedBody.raw === null ||
    typeof parsedBody.raw !== "object" ||
    Array.isArray(parsedBody.raw)
  ) {
    return NextResponse.json({ error: "malformed_body" }, { status: 400 });
  }
  const result = await targetFetch<Record<string, unknown>>(
    `/workspaces/${id}/category-mix/preview`,
    token,
    { method: "POST", body: JSON.stringify(parsedBody.raw), resourceMetadata: true },
  );
  if (!result.ok) return passThrough(result);
  return NextResponse.json(result.data);
}
