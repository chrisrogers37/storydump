import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";
import {
  EXPIRY_DAYS_DEFAULT,
  expiryDaysValid,
  mintedTokenFrom,
  tokenNameValid,
} from "@/lib/tokens";

/**
 * `/api/workspaces/[id]/tokens` — the WORKSPACE's service identities (CLI v2
 * phase 01, spec §2). Admin floor is the API's: a member is answered 403,
 * which reaches the browser as `http_403` (the API's plain `{detail}`
 * carries no reason) or `insufficient_role`, and the copy names the floor.
 *
 * A token is a RESOURCE, so this is REST and not a command (F1 (b),
 * `sources/route.ts`). No `Idempotency-Key`: two mints are two identities.
 *
 * NO ROLE IS FORWARDED, and that is the point of this route rather than a
 * gap in it. A service identity reads only in this release — the API fixes
 * `readonly` and its body has no `role` field — so the body is
 * `{name, expires_in_days}` and a `role` the browser sends is dropped here.
 * A write-capable service principal is a later slice, and it arrives by
 * changing this comment, not by a body that happened to pass one through.
 */

/** POST — mint one. Body `{name, expires_in_days}`; the secret comes back once. */
export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  const token = await getSessionToken();
  if (!token)
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  const { id } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return NextResponse.json({ error: "malformed_body" }, { status: 400 });
  }

  const body = raw as { name?: unknown; expires_in_days?: unknown } | null;
  const name = typeof body?.name === "string" ? body.name.trim() : "";
  if (!tokenNameValid(name)) {
    return NextResponse.json({ error: "invalid_name" }, { status: 400 });
  }
  const expiresInDays =
    body?.expires_in_days === undefined
      ? EXPIRY_DAYS_DEFAULT
      : body.expires_in_days;
  if (!expiryDaysValid(expiresInDays)) {
    return NextResponse.json({ error: "invalid_expiry" }, { status: 400 });
  }

  const result = await targetFetch<Record<string, unknown>>(
    `/workspaces/${id}/tokens`,
    token,
    {
      method: "POST",
      body: JSON.stringify({ name, expires_in_days: expiresInDays }),
    },
  );
  if (!result.ok) {
    return NextResponse.json(
      { error: result.error },
      { status: result.status },
    );
  }

  const minted = mintedTokenFrom(result.data);
  if (!minted) {
    return NextResponse.json({ error: "malformed_response" }, { status: 502 });
  }
  return NextResponse.json(minted, { status: 201 });
}
