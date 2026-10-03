import { NextRequest, NextResponse } from "next/server";
import { passThrough, refuseCrossSite, requireSessionToken } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/** The API's answers to an unlink (094, `07` §37); `last_identity` is a 409. */
const UNLINK_OUTCOMES = new Set(["unlinked", "not_linked"]);

/**
 * DELETE /api/me/telegram — the signed-in user removes their own Telegram
 * identity. Tenant-less, like `me/telegram/link`, the link it reverses.
 * `not_linked` is a success too: the person asked for no Telegram on their
 * account, and that is how it stands. Memberships stay; taps from that
 * Telegram account count for nobody until the person links again.
 */
export async function DELETE(request: NextRequest) {
  const refused = refuseCrossSite(request);
  if (refused) return refused;

  const token = await requireSessionToken();
  if (token instanceof NextResponse) return token;

  const result = await targetFetch<{ outcome?: unknown }>("/me/telegram", token, {
    method: "DELETE",
  });
  if (!result.ok) return passThrough(result);

  const outcome = result.data?.outcome;
  if (typeof outcome !== "string" || !UNLINK_OUTCOMES.has(outcome)) {
    return NextResponse.json({ error: "malformed_response" }, { status: 502 });
  }
  return NextResponse.json({ outcome });
}
