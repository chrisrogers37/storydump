import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isUuid, isWorkspaceId } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";
import { isInstagramAuthorizationUrl } from "@/lib/destination";

/**
 * POST /api/workspaces/[id]/accounts/[accountId]/connect — start the Instagram
 * Login grant for ONE destination and hand back where the browser goes
 * (#1220 step 2). The Drive connect proxy's shape, exactly.
 *
 * Not a command: an OAuth leg is a browser redirect the command port cannot
 * express, so the API serves it as a resource route and this proxy mirrors
 * it. No `Idempotency-Key`: minting a state is last-issued-wins by design.
 *
 * Per-DESTINATION, because the credential attaches to the `ig_accounts` row
 * the person typed, flipping its provisional `manual:<handle>` reference to
 * the real Meta id. The API answers 404 — never 403 — for a destination that
 * is not this workspace's, and that passes through unchanged.
 */
export async function POST(
  _request: NextRequest,
  context: { params: Promise<{ id: string; accountId: string }> },
) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  const { id, accountId } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }
  if (!isUuid(accountId)) {
    return NextResponse.json({ error: "invalid_account" }, { status: 400 });
  }

  const result = await targetFetch<{ authorization_url?: string }>(
    `/workspaces/${id}/accounts/${accountId}/connect`,
    token,
    { method: "POST" },
  );

  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }

  const url = result.data?.authorization_url;
  if (typeof url !== "string" || !isInstagramAuthorizationUrl(url)) {
    // A 200 whose body is not a usable redirect is a failure, not a success
    // with a missing field — the caller's next act is to navigate.
    return NextResponse.json({ error: "malformed_authorization_url" }, { status: 502 });
  }

  return NextResponse.json({ authorizationUrl: url });
}
