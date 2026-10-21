import { NextRequest, NextResponse } from "next/server";
import { isUuid } from "@/lib/session";
import { readJsonObject, refuseCrossSite, requireWorkspace } from "@/lib/route-guards";
import { proxyStartOfGrant } from "@/lib/start-proxy";
import { isInstagramAuthorizationUrl } from "@/lib/destination";

/**
 * POST /api/workspaces/[id]/accounts/[accountId]/connect — start the Instagram
 * Login grant for ONE destination and hand back where the browser goes
 * (#1220). The Drive connect proxy's shape, exactly.
 *
 * Not a command: an OAuth leg is a browser redirect the command port cannot
 * express, so the API serves it as a resource route and this proxy mirrors
 * it. No `Idempotency-Key`: minting a state is last-issued-wins by design.
 *
 * Per-DESTINATION: the credential attaches to the row this route names —
 * connect or reconnect — and a row still on its typed `manual:<handle>`
 * reference flips to the real Meta id. Adding a NEW destination is the
 * workspace-level sibling (`../connect`). The API answers 404 — never 403 —
 * for a destination that is not this workspace's, and that passes through
 * unchanged.
 *
 * The body is forwarded unchanged, as on the sibling: a first connect may
 * carry its history choice, and a reconnect sends `{"past_posts_choice": null}`
 * (#1644).
 */
export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string; accountId: string }> },
) {
  const refused = refuseCrossSite(request);
  if (refused) return refused;

  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;

  const { accountId } = await context.params;
  if (!isUuid(accountId)) {
    return NextResponse.json({ error: "invalid_account" }, { status: 400 });
  }
  const body = await readJsonObject(request);
  if (body instanceof NextResponse) return body;

  return proxyStartOfGrant(
    `/workspaces/${id}/accounts/${accountId}/connect`,
    token,
    isInstagramAuthorizationUrl,
    body,
  );
}
