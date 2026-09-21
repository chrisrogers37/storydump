import { NextResponse } from "next/server";
import { passThrough, requireSessionToken } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";
import {
  LINK_TTL_SECONDS_FALLBACK,
  isTelegramLink,
} from "@/lib/telegram-link";

/**
 * POST /api/me/telegram/link — mint the one-shot Telegram deep link for the
 * signed-in user (#1172 clause-1 wiring). Tenant-less: an identity belongs to
 * a user, not a workspace, so there is no workspace segment and no
 * `Idempotency-Key` — every click is a fresh, independent link.
 */
export async function POST() {
  const token = await requireSessionToken();
  if (token instanceof NextResponse) return token;

  const result = await targetFetch<{ link?: string; expires_in_seconds?: number }>(
    "/me/telegram/link",
    token,
    { method: "POST" },
  );

  if (!result.ok) return passThrough(result);

  const link = result.data?.link;
  if (typeof link !== "string" || !isTelegramLink(link)) {
    // A 200 whose body is not a usable link is a failure: the caller's next
    // act is to open it.
    return NextResponse.json({ error: "malformed_link" }, { status: 502 });
  }

  return NextResponse.json({
    link,
    // The API's STATE_TTL_SECONDS, named once as `LINK_TTL_SECONDS_FALLBACK`;
    // the fallback is the same number, never 0.
    expiresInSeconds: result.data?.expires_in_seconds ?? LINK_TTL_SECONDS_FALLBACK,
  });
}
