import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";

/**
 * POST /api/workspaces/[id]/sources — add a Drive folder as a media source
 * (epic P4; the target route is #1053's `POST /workspaces/{ws}/sources`).
 *
 * ## Why this is REST and not a command
 *
 * F1 locked (b): commands for verbs on workspace state, REST for resources —
 * the split #1053 already made. A Drive folder is a resource and the
 * vocabulary has no name for creating one, so routing it through the command
 * client to be tidier would mean amending a vocabulary F1 locked against
 * amending lightly. `sync_now`, a verb, goes the other way in the same change.
 *
 * No `Idempotency-Key`, and not by omission: the target route is idempotent on
 * the FOLDER by construction, under an advisory lock
 * (`get_or_create_media_source`). A second submit of the same folder returns
 * the same source at 200 rather than 201. There is nothing for a submission
 * key to add.
 *
 * ## What it does NOT return
 *
 * `{source_id, created}` and nothing else. It deliberately does not read the
 * folder — that is the Drive seam (#982) and a separate build — so a caller
 * cannot render a file count or a category list from this, and must not
 * pretend to. The control this replaced did exactly that against the legacy
 * route.
 */
export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

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

  const body = raw as { folder_ref?: unknown; root_name?: unknown };
  const folderRef = typeof body?.folder_ref === "string" ? body.folder_ref.trim() : "";
  if (!folderRef) {
    return NextResponse.json({ error: "folder_required" }, { status: 400 });
  }

  // Shape only. WHAT a valid folder reference is belongs to the port, which
  // accepts a bare id or a folder URL and refuses anything else BY NAME
  // (`folder_ref_from`). A second copy of that rule here is the copy that
  // would go stale — and it is a rule with a documented trap, where a
  // markerless URL used to reduce to `https:` and silently merge two unrelated
  // folders onto one source.
  const rootName = typeof body?.root_name === "string" ? body.root_name.trim() : "";

  const result = await targetFetch<{ source_id?: string; created?: boolean }>(
    `/workspaces/${id}/sources`,
    token,
    {
      method: "POST",
      body: JSON.stringify(
        rootName ? { folder_ref: folderRef, root_name: rootName } : { folder_ref: folderRef },
      ),
    },
  );

  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }

  const sourceId = result.data?.source_id;
  if (typeof sourceId !== "string") {
    return NextResponse.json({ error: "malformed_response" }, { status: 502 });
  }

  // `created` distinguishes a new source from the same folder submitted twice.
  // Passed through rather than flattened: "added" and "you already had this
  // one" are different sentences, and only the caller can say them.
  return NextResponse.json({ sourceId, created: result.data?.created === true });
}
