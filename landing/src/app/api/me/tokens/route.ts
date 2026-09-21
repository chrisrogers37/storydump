import { NextRequest, NextResponse } from "next/server";
import {
  passThrough,
  readJsonBody,
  requireSessionToken,
} from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";
import {
  EXPIRY_DAYS_DEFAULT,
  expiryDaysValid,
  isTokenRole,
  mintedTokenFrom,
  tokenNameValid,
} from "@/lib/tokens";

/**
 * `/api/me/tokens` — the signed-in person's own API tokens (CLI v2 phase 01,
 * spec §2). Tenant-less, like `me/telegram/link`: a person-bound token
 * belongs to a user, not a workspace, so there is no workspace segment.
 *
 * A token is a RESOURCE, so this is REST and not a command — F1 (b), the
 * split `sources/route.ts` explains — and there is no `Idempotency-Key`:
 * two mints are two tokens by design, each with its own secret.
 *
 * Only a SESSION can be here. The API refuses a token principal on this
 * route (a token never mints a token); this tier only ever forwards the
 * session cookie, so that gate is never even reached from the browser.
 */

/**
 * POST — mint one. Body `{name, role, expires_in_days}`, the API's own
 * shape; the SECRET comes back exactly once, in this response, and this
 * tier never stores or logs it.
 *
 * The shape is checked here so a blank name is a field error next to the
 * input rather than a 400 from upstream the copy can only half-name. WHAT a
 * legal value is beyond the shape stays the API's.
 */
export async function POST(request: NextRequest) {
  const token = await requireSessionToken();
  if (token instanceof NextResponse) return token;

  const parsedBody = await readJsonBody(request);
  if (parsedBody instanceof NextResponse) return parsedBody;

  const body = parsedBody.raw as {
    name?: unknown;
    role?: unknown;
    expires_in_days?: unknown;
  } | null;
  const name = typeof body?.name === "string" ? body.name.trim() : "";
  if (!tokenNameValid(name)) {
    return NextResponse.json({ error: "invalid_name" }, { status: 400 });
  }
  const role = body?.role;
  if (!isTokenRole(role)) {
    return NextResponse.json({ error: "invalid_role" }, { status: 400 });
  }
  // Absent is the API's default; present-and-wrong is refused by name.
  const expiresInDays =
    body?.expires_in_days === undefined
      ? EXPIRY_DAYS_DEFAULT
      : body.expires_in_days;
  if (!expiryDaysValid(expiresInDays)) {
    return NextResponse.json({ error: "invalid_expiry" }, { status: 400 });
  }

  const result = await targetFetch<Record<string, unknown>>(
    "/me/tokens",
    token,
    {
      method: "POST",
      body: JSON.stringify({ name, role, expires_in_days: expiresInDays }),
    },
  );
  if (!result.ok) return passThrough(result);

  // A 201 whose body carries no usable secret is a failure: the caller's
  // next act is to paste it.
  const minted = mintedTokenFrom(result.data);
  if (!minted) {
    return NextResponse.json({ error: "malformed_response" }, { status: 502 });
  }
  return NextResponse.json(minted, { status: 201 });
}
