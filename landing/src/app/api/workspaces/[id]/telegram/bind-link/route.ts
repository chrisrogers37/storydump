import { NextRequest, NextResponse } from "next/server";
import { passThrough, requireWorkspace } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";
import {
  LINK_TTL_SECONDS_FALLBACK,
  isTelegramGroupLink,
} from "@/lib/telegram-link";

/**
 * POST /api/workspaces/[id]/telegram/bind-link — mint the one-shot link that
 * opens Telegram's group picker and binds the chosen group to this workspace
 * (`07` §13). Admin floor is the API's; a link that is not our bot's
 * `startgroup` link is a failure, because the next act is to open it.
 */
export async function POST(_request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;

  const result = await targetFetch<{ link?: string; expires_in_seconds?: number }>(
    `/workspaces/${id}/telegram/bind-link`,
    token,
    { method: "POST" },
  );
  if (!result.ok) return passThrough(result);
  const link = result.data?.link;
  if (typeof link !== "string" || !isTelegramGroupLink(link)) {
    return NextResponse.json({ error: "malformed_link" }, { status: 502 });
  }
  return NextResponse.json({
    link,
    expiresInSeconds: result.data?.expires_in_seconds ?? LINK_TTL_SECONDS_FALLBACK,
  });
}
