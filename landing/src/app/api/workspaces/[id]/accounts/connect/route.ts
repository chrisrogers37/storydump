import { NextRequest, NextResponse } from "next/server";
import { readJsonObject, refuseCrossSite, requireWorkspace } from "@/lib/route-guards";
import { proxyStartOfGrant } from "@/lib/start-proxy";
import { isInstagramAuthorizationUrl } from "@/lib/destination";

/**
 * POST /api/workspaces/[id]/accounts/connect — start the Instagram grant that
 * ADDS a destination: no account is named, the
 * callback lands the signed-in Instagram account on the row it already has
 * here or on a new one. The per-destination sibling connects an existing row.
 *
 * The body is forwarded unchanged: `{}` is the empty start, and
 * `{"archive_choice": …}` carries the history choice the connect dialog made
 * (#1644). The API refuses a choice by name; a stale one's 409 keeps
 * its revision on the way back.
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
  const body = await readJsonObject(request);
  if (body instanceof NextResponse) return body;

  return proxyStartOfGrant(
    `/workspaces/${id}/accounts/connect`,
    token,
    isInstagramAuthorizationUrl,
    body,
  );
}
