import { NextRequest, NextResponse } from "next/server";
import {
  SESSION_COOKIE,
  WORKSPACE_COOKIE,
  getSessionToken,
} from "@/lib/session";
import { targetFetch } from "@/lib/target-api";

/**
 * Sign out — revoke server-side, THEN clear the cookie.
 *
 * Clearing the cookie is not a logout. Under the old self-contained JWT it was
 * the only thing available, and it meant a copied token stayed valid until it
 * expired; `session_tokens.revoked_at` is the column that makes sign-out mean
 * something, and this is the only place that writes it.
 *
 * The cookie is cleared even when revocation fails. The alternative — refusing
 * to sign out because the router is unreachable — leaves someone signed in at a
 * shared machine because of an outage they cannot see. The local half always
 * happens; the durable half is attempted and its failure is not the user's to
 * resolve.
 */
async function signOut(request: NextRequest) {
  const token = await getSessionToken();

  if (token) {
    await targetFetch("/auth/logout", token, { method: "POST" });
  }

  const response = NextResponse.redirect(new URL("/login", request.url));
  response.cookies.delete(SESSION_COOKIE);
  response.cookies.delete(WORKSPACE_COOKIE);
  return response;
}

export const GET = signOut;
export const POST = signOut;
