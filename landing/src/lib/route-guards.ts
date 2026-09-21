import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId } from "./session";

/**
 * The four things every route handler on this tier does before it does
 * anything: prove there is a session, prove the workspace segment is an id,
 * read a JSON body, and relay a refusal the API already made.
 *
 * NINETEEN COPIES (eighteen after #1338 deleted the BFF proxy). The
 * duplication was not the problem; the VOCABULARY was. `unauthenticated`,
 * `invalid_workspace` and `malformed_body` are strings the browser switches
 * on — `refusalCopy`, `settingsRefusalCopy`, `createWorkspaceRefusalCopy`,
 * `destinationConnectRefusalCopy` and `driveRefusalCopy` all have a `case`
 * for one or more of them — and one route answering `unauthorised` or
 * `invalid_workspace_id` would miss every branch and fall to a generic
 * sentence that names no remedy. Nothing about the strings or the statuses
 * changes here; they move to where they cannot be mistyped.
 *
 * EACH GUARD RETURNS ITS VALUE OR A RESPONSE, and the caller narrows with
 * `instanceof NextResponse`. Deliberately not a thrown error: a route
 * handler that throws is a 500 in Next's own words, and "not signed in" is
 * an answer, not a fault.
 */

/** The session token, or the 401 every route answers without one. */
export async function requireSessionToken(): Promise<string | NextResponse> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }
  return token;
}

/** The session token AND the workspace id, or the 401/400 that stops the route. */
export async function requireWorkspace(context: {
  params: Promise<{ id: string }>;
}): Promise<{ token: string; id: string } | NextResponse> {
  const token = await requireSessionToken();
  if (token instanceof NextResponse) return token;
  const { id } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }
  return { token, id };
}

/**
 * Relay a refusal the API already made, unchanged.
 *
 * The API's reason and status ride through verbatim — a 409
 * `illegal_transition` is a normal answer this tier has no opinion about,
 * and re-coding it here is how the two tiers come to disagree about what
 * happened.
 */
export function passThrough(result: {
  error: string;
  status: number;
}): NextResponse {
  return NextResponse.json({ error: result.error }, { status: result.status });
}

/** The request's JSON body, or the 400 for a body that is not JSON. */
export async function readJsonBody(
  request: NextRequest,
): Promise<{ raw: unknown } | NextResponse> {
  try {
    return { raw: await request.json() };
  } catch {
    return NextResponse.json({ error: "malformed_body" }, { status: 400 });
  }
}
