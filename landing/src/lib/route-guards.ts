import { NextRequest, NextResponse } from "next/server";
import type { RefusalFacts } from "./refusal-facts";
import { getSessionToken, isWorkspaceId } from "./session";

/**
 * The five things every route handler on this tier does before it does
 * anything: refuse a request another site made, prove there is a session,
 * prove the workspace segment is an id, read a JSON body, and relay a refusal
 * the API already made.
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

/**
 * The 403 for a state-changing request that this origin's own pages did not
 * make, or null to carry on. It runs FIRST in every POST, PUT, PATCH and
 * DELETE handler, which `cross-site-guard-contract.test.ts` holds.
 *
 * WHY THE SESSION COOKIE IS NOT ENOUGH. The API sets it `SameSite=Lax`, which
 * keeps it off a cross-SITE post. A site is the registrable domain, so every
 * subdomain of the product domain is the same site: a page on any sibling
 * host would post with the cookie attached.
 *
 * `Sec-Fetch-Site` is the browser's own verdict and cannot be set by a page,
 * so when it is present it decides, and only `same-origin` passes. A browser
 * that predates it still sends `Origin` on a post; that must equal this
 * request's origin whole, never by prefix. A request with neither header is
 * not from a browser, so it carries no victim's cookie and is not a forgery.
 *
 * In the handler rather than in middleware: the middleware matcher does not
 * cover `/api`, and a matcher bypass would skip it anyway.
 */
export function refuseCrossSite(request: NextRequest): NextResponse | null {
  const site = request.headers.get("sec-fetch-site");
  if (site !== null) {
    return site === "same-origin" ? null : crossSite();
  }
  const origin = request.headers.get("origin");
  if (origin !== null && origin !== request.nextUrl.origin) {
    return crossSite();
  }
  return null;
}

function crossSite(): NextResponse {
  return NextResponse.json({ error: "cross_site" }, { status: 403 });
}

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
 *
 * A command refusal's facts ride beside the reason when the result carries
 * them. They are already through the allow-list (`refusal-facts.ts`), and only
 * the command route asks for them.
 */
export function passThrough(result: {
  error: string;
  status: number;
  facts?: RefusalFacts;
}): NextResponse {
  return NextResponse.json(
    result.facts ? { error: result.error, facts: result.facts } : { error: result.error },
    { status: result.status },
  );
}

/**
 * The request's JSON body, or the 400 for a body that is not JSON.
 *
 * The body must be DECLARED `application/json` as well as parse. A plain HTML
 * form can post `text/plain` with no CORS preflight, and `request.json()`
 * parses such a body as readily as a JSON one. Every caller on this tier
 * sends `application/json` (`postJson` in `bff.ts`).
 */
export async function readJsonBody(
  request: NextRequest,
): Promise<{ raw: unknown } | NextResponse> {
  const type = request.headers.get("content-type")?.split(";")[0].trim().toLowerCase();
  if (type !== "application/json") {
    return NextResponse.json({ error: "malformed_body" }, { status: 400 });
  }
  try {
    return { raw: await request.json() };
  } catch {
    return NextResponse.json({ error: "malformed_body" }, { status: 400 });
  }
}
