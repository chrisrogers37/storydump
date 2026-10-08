import { NextRequest, NextResponse } from "next/server";
import { requireWorkspace } from "@/lib/route-guards";
import { isUuid } from "@/lib/session";
import { targetFetchBytes } from "@/lib/target-api";
import { THUMBNAIL_TYPES } from "@/lib/thumbnails";

/**
 * GET /api/workspaces/[id]/media/[mediaId]/thumbnail — a media item's
 * thumbnail, streamed from the API, which fetched it from the provider behind
 * the workspace check. The page's URL carries the item's version
 * (`thumbnailSrc`), which only keys the browser's cache, and the API's
 * `Cache-Control` rides through. A failure is a bare status that is never
 * kept, and the page draws its placeholder in the picture's place.
 */
export async function GET(
  _request: NextRequest,
  context: { params: Promise<{ id: string; mediaId: string }> },
) {
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;
  const { mediaId } = await context.params;
  if (!isUuid(mediaId)) {
    return NextResponse.json({ error: "invalid_media" }, { status: 400 });
  }
  const upstream = await targetFetchBytes(
    `/workspaces/${id}/media/${mediaId}/thumbnail`,
    token,
  );
  const type =
    upstream.headers.get("content-type")?.split(";")[0].trim().toLowerCase() ?? "";
  if (!upstream.ok || !THUMBNAIL_TYPES.includes(type)) {
    await upstream.body?.cancel();
    return new NextResponse(null, {
      status: upstream.ok ? 502 : upstream.status,
      headers: { "Cache-Control": "no-store" },
    });
  }
  return new NextResponse(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": type,
      "Cache-Control": upstream.headers.get("cache-control") ?? "no-store",
    },
  });
}
