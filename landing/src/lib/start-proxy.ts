import { NextResponse } from "next/server";
import { targetFetch } from "./target-api";

/**
 * The body every "start a provider grant" proxy shares: ask the API for the
 * URL the browser goes to, refuse a 200 whose body is not a usable redirect
 * (the caller's next act is to navigate, so a missing field is a failure, not
 * a success), and hand back `{ authorizationUrl }`. Param validation and the
 * design rationale stay in each route — this is only the mechanical half.
 */
export async function proxyStartOfGrant(
  path: string,
  token: string,
  isAllowedUrl: (value: string) => boolean,
): Promise<NextResponse> {
  const result = await targetFetch<{ authorization_url?: string }>(path, token, {
    method: "POST",
  });

  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }

  const url = result.data?.authorization_url;
  if (typeof url !== "string" || !isAllowedUrl(url)) {
    return NextResponse.json({ error: "malformed_authorization_url" }, { status: 502 });
  }

  return NextResponse.json({ authorizationUrl: url });
}
