import { NextRequest, NextResponse } from "next/server";
import { passThrough, refuseCrossSite, requireSessionToken } from "@/lib/route-guards";
import { isUuid } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";

/**
 * POST /api/me/invitations/[invitationId]/resend — "Email me the link again"
 * (invitee invitation functions).
 *
 * The API mints a fresh link for one of the signed-in person's invitations and
 * emails it to the invited address; the old link stops working. The answer is
 * the email's `delivery` and never the link, which exists only in the email.
 * An invitation that is not theirs, or no longer open, is the API's one 404.
 */
export async function POST(
  request: NextRequest,
  context: { params: Promise<{ invitationId: string }> },
) {
  const refused = refuseCrossSite(request);
  if (refused) return refused;

  const token = await requireSessionToken();
  if (token instanceof NextResponse) return token;

  // A path segment on the next hop, so only an id's shape goes through.
  const { invitationId } = await context.params;
  if (!isUuid(invitationId)) {
    return NextResponse.json({ error: "invalid_invitation" }, { status: 400 });
  }

  const result = await targetFetch<Record<string, unknown>>(
    `/me/invitations/${invitationId}/resend`,
    token,
    { method: "POST" },
  );
  if (!result.ok) return passThrough(result);
  return NextResponse.json({ delivery: result.data.delivery ?? null });
}
