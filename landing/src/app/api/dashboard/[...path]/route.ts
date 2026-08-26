/**
 * BFF proxy — forwards /api/dashboard/* to the target router.
 *
 * ── What this no longer has to do ──────────────────────────────────────────
 *
 * The version this replaces carried a membership re-check: it fetched the
 * user's instances on every proxied call and, if the active chat was no longer
 * among them, re-minted the session with a null chat id. That existed because a
 * self-contained JWT cannot be revoked — a user removed from a group kept a
 * valid token until it expired, so the only defence was to re-ask on every
 * request and hope the check ran.
 *
 * The target session is a `session_tokens` row and every target route
 * authorizes against `workspace_members` server-side, so removal takes effect
 * on the next call without anyone re-asking. The check is deleted rather than
 * ported: a second copy of an authorization decision is one that can disagree
 * with the first, and this one failed OPEN by design ("backend unreachable —
 * fall through") in exactly the situation where it mattered.
 *
 * What is kept, unchanged, is the path allowlist and the traversal guard. Those
 * are about which endpoints this door may reach at all, which is still this
 * tier's question.
 */

import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId, WORKSPACE_COOKIE } from "@/lib/session";
import { TARGET_API_URL } from "@/lib/target-api";

/** Allowlisted target path prefixes. Prevents traversal to arbitrary routes. */
const ALLOWED_PATHS = [
  "analytics",
  "accounts",
  "audit-log",
  "category-mix",
  "history-detail",
  "media",
  "media-library",
  "media-stats",
  "queue-detail",
  "queue-preview",
  "system-status",
  "toggle-setting",
  "update-setting",
  "update-string-setting",
  "update-category-mix",
  "switch-account",
  "sync-media",
  "init",
  "schedule",
  "complete",
  "media-folder",
  "start-indexing",
  "add-account",
  "remove-account",
  "disconnect-gdrive",
];

async function proxyRequest(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const sessionToken = await getSessionToken();
  if (!sessionToken) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const workspaceId = request.cookies.get(WORKSPACE_COOKIE)?.value;
  if (!isWorkspaceId(workspaceId)) {
    return NextResponse.json({ error: "no_workspace_selected" }, { status: 422 });
  }

  const { path } = await params;

  if (path.some((segment) => segment === ".." || segment === ".")) {
    return NextResponse.json({ error: "invalid_path" }, { status: 400 });
  }

  const topSegment = path[0];
  if (!topSegment || !ALLOWED_PATHS.includes(topSegment)) {
    return NextResponse.json({ error: "invalid_path" }, { status: 400 });
  }

  const prefix = `/api/v1/workspaces/${workspaceId}/`;
  const url = new URL(`${prefix}${path.join("/")}`, TARGET_API_URL);

  // Belt-and-suspenders: the resolved URL must still sit under this workspace.
  if (!url.pathname.startsWith(prefix)) {
    return NextResponse.json({ error: "invalid_path" }, { status: 400 });
  }

  new URL(request.url).searchParams.forEach((value, key) => {
    url.searchParams.set(key, value);
  });

  const headers = new Headers({ Authorization: `Bearer ${sessionToken}` });
  const fetchOptions: RequestInit = { method: request.method, headers };

  if (request.method !== "GET" && request.method !== "HEAD") {
    // Forwarded verbatim. The credential rides the Authorization header, so
    // there is nothing to inject into the body — which is what the old proxy
    // did, and why a body could contradict its own envelope.
    headers.set("Content-Type", "application/json");
    fetchOptions.body = await request.text();
  }

  try {
    const response = await fetch(url.toString(), fetchOptions);

    if (response.status >= 500) {
      console.error("target 5xx:", response.status, url.pathname);
      return NextResponse.json({ error: "upstream_unavailable" }, { status: 502 });
    }

    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      return NextResponse.json(await response.json(), { status: response.status });
    }
    return new NextResponse(await response.text(), {
      status: response.status,
      headers: { "Content-Type": contentType },
    });
  } catch (error) {
    console.error("BFF proxy error:", error);
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const PATCH = proxyRequest;
export const DELETE = proxyRequest;
