import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isUuid, isWorkspaceId } from "@/lib/session";
import { proxyStartOfGrant } from "@/lib/start-proxy";
import { isAuthorizationUrl } from "@/lib/source-connect";

/**
 * POST /api/workspaces/[id]/sources/[sourceId]/connect — start the Drive grant
 * for ONE source and hand back where the browser goes (gdrive epic P3, #1065).
 *
 * ## Why this is not a command
 *
 * An OAuth leg is a browser redirect, which the command port cannot express,
 * so the API serves it as a resource route rather than a command — and this
 * proxy mirrors that. It carries no `Idempotency-Key`: there is nothing to
 * dedup, because minting a state is deliberately last-issued-wins (a second
 * click retires the first state rather than replaying it).
 *
 * ## Why per-source, which is the whole reason the old button was deleted
 *
 * A Drive credential is per-SOURCE — `ck_credentials_one_owner` ties it to a
 * `media_source_id`, and a workspace can hold one source per Drive folder
 * (`get_or_create_media_source` is idempotent on the folder, and its own note
 * says creates for different folders never contend). The control this replaces
 * called `oauth-url/<provider>`, which was per-WORKSPACE and therefore had no
 * answer to *which* source it was granting. That is why #1070 deleted it
 * rather than leaving it disabled, and why this is a build rather than a
 * re-enable.
 *
 * The API answers 404 — never 403 — for a source that is not this workspace's,
 * because a source's existence is not disclosed across tenants. That passes
 * through unchanged.
 */

export async function POST(
  _request: NextRequest,
  context: { params: Promise<{ id: string; sourceId: string }> },
) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  const { id, sourceId } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }
  // Shape only, and not an authorization check: it stops a junk value becoming
  // a path segment. Whether this source belongs to this workspace is the API's
  // question and it answers 404.
  if (!isUuid(sourceId)) {
    return NextResponse.json({ error: "invalid_source" }, { status: 400 });
  }

  return proxyStartOfGrant(`/workspaces/${id}/sources/${sourceId}/connect`, token, isAuthorizationUrl);
}
