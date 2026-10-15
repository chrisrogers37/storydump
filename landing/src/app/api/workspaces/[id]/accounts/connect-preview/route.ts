import { NextRequest, NextResponse } from "next/server";
import { isUuid } from "@/lib/session";
import { passThrough, requireWorkspace } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * GET /api/workspaces/[id]/accounts/connect-preview?account_id=… — what an
 * Instagram connect would do before it starts (#1644): whether past
 * posts are a choice here, the entry (`add`, `first_connect`, `reconnect`),
 * the switch's default or the saved setting a reconnect keeps, the revision a
 * choice is checked against, and the equal shares an On choice would leave.
 * The API writes nothing for it.
 *
 * Without `account_id` it previews the header's Add; with one, a row's
 * connect, whose account the API answers 404 for when it is not this
 * workspace's. The guards are the sibling start routes', less the cross-site
 * check a read does not need, and the answer comes back whole.
 */
export async function GET(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;
  const accountId = request.nextUrl.searchParams.get("account_id");
  if (accountId !== null && !isUuid(accountId)) {
    return NextResponse.json({ error: "invalid_account" }, { status: 400 });
  }
  const query = accountId === null ? "" : `?account_id=${accountId}`;
  const result = await targetFetch<Record<string, unknown>>(
    `/workspaces/${id}/accounts/connect-preview${query}`,
    token,
    { resourceMetadata: true },
  );
  if (!result.ok) return passThrough(result);
  return NextResponse.json(result.data);
}
