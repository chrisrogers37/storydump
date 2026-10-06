import { NextRequest, NextResponse } from "next/server";
import { isUuid } from "@/lib/session";
import { passThrough, refuseCrossSite, requireWorkspace } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * DELETE /api/workspaces/[id]/bindings/[bindingId] — remove a Telegram group
 * from the workspace (`07` §13). The API REVOKES the binding rather than
 * deleting it: cards still queued for the group are dropped, cards already
 * posted stay in its history, and a fresh bind link brings it back. Admin
 * floor is the API's.
 */
export async function DELETE(
  request: NextRequest,
  context: { params: Promise<{ id: string; bindingId: string }> },
) {
  const refused = refuseCrossSite(request);
  if (refused) return refused;

  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;
  const { bindingId } = await context.params;
  if (!isUuid(bindingId)) {
    return NextResponse.json({ error: "invalid_binding" }, { status: 400 });
  }
  const result = await targetFetch<{ binding_id?: string; state?: string }>(
    `/workspaces/${id}/bindings/${bindingId}`,
    token,
    { method: "DELETE" },
  );
  if (!result.ok) return passThrough(result);
  return NextResponse.json({ bindingId, state: result.data?.state ?? "revoked" });
}
